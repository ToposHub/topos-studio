"""
Tests for track visibility rendering.

Verifies:
- is_track_renderable respects is_visible, is_enabled, is_muted, is_solo
- Hidden tracks excluded from render plans
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.timeline_render_plan import is_track_renderable, TimelineRenderPlan
from src.shared.media.metadata import Rational


def _make_clip(clip_id: str = "c1", track_id: str = "v1",
               start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


def _make_track(track_id: str = "v1", name: str = "V1",
                track_type: str = "video", index: int = 0, clips=None,
                is_visible: bool = True, is_enabled: bool = True,
                is_muted: bool = False, is_solo: bool = False) -> EditTrack:
    return EditTrack(
        track_id=track_id, name=name, track_type=track_type, index=index,
        clips=tuple(clips) if clips else (),
        is_visible=is_visible, is_enabled=is_enabled,
        is_muted=is_muted, is_solo=is_solo,
    )


def _make_timeline(video_tracks=None, audio_tracks=None) -> TimelineSequence:
    v = tuple(video_tracks) if video_tracks else (_make_track(),)
    a = tuple(audio_tracks) if audio_tracks else ()
    return TimelineSequence(
        timeline_id="tl1", name="Test", fps=Rational(24, 1),
        video_tracks=v, audio_tracks=a,
    )


class TestIsTrackRenderable:
    """Verify is_track_renderable logic."""

    def test_visible_enabled_track(self):
        track = _make_track()
        assert is_track_renderable(track, has_solo=False) is True

    def test_hidden_track_not_renderable(self):
        track = _make_track(is_visible=False)
        assert is_track_renderable(track, has_solo=False) is False

    def test_disabled_track_not_renderable(self):
        track = _make_track(is_enabled=False)
        assert is_track_renderable(track, has_solo=False) is False

    def test_muted_track_not_renderable(self):
        track = _make_track(is_muted=True)
        assert is_track_renderable(track, has_solo=False) is False

    def test_solo_track_renderable_when_has_solo(self):
        track = _make_track(is_solo=True)
        assert is_track_renderable(track, has_solo=True) is True

    def test_non_solo_track_not_renderable_when_has_solo(self):
        track = _make_track(is_solo=False)
        assert is_track_renderable(track, has_solo=True) is False

    def test_hidden_overrides_solo(self):
        track = _make_track(is_visible=False, is_solo=True)
        assert is_track_renderable(track, has_solo=True) is False

    def test_hidden_overrides_enabled(self):
        track = _make_track(is_visible=False, is_enabled=True)
        assert is_track_renderable(track, has_solo=False) is False


class TestHiddenTrackNotInRenderPlan:
    """Verify hidden tracks are excluded from render plans."""

    def test_visible_track_in_plan(self):
        clip = _make_clip()
        track = _make_track(clips=[clip])
        tl = _make_timeline(video_tracks=[track])
        plan = TimelineRenderPlan(tl)
        frame_plan = plan.get_frame_plan(0)
        # Should have a layer for the visible clip
        assert len(frame_plan.layers) > 0

    def test_hidden_track_excluded_from_plan(self):
        clip = _make_clip()
        track = _make_track(clips=[clip], is_visible=False)
        tl = _make_timeline(video_tracks=[track])
        plan = TimelineRenderPlan(tl)
        frame_plan = plan.get_frame_plan(0)
        assert len(frame_plan.layers) == 0

    def test_mixed_visible_hidden_tracks(self):
        c1 = _make_clip("c1", "v1", 0, 100)
        c2 = _make_clip("c2", "v2", 0, 100)
        v1 = _make_track("v1", "V1", clips=[c1], is_visible=True)
        v2 = _make_track("v2", "V2", clips=[c2], is_visible=False, index=1)
        tl = _make_timeline(video_tracks=[v1, v2])
        plan = TimelineRenderPlan(tl)
        frame_plan = plan.get_frame_plan(0)
        # Only the visible track's clip should appear
        assert len(frame_plan.layers) == 1
        assert frame_plan.layers[0].clip_id == "c1"
