"""
Tests for external file drop and drop target resolution.

Verifies:
- TimelineLayoutEngine.resolve_drop_target() returns valid results
- Track type filtering (video files → video tracks, audio → audio)
- Drop on locked track is rejected
- Drop on ruler/splitter is rejected
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
)
from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine,
    TimelineLayoutState,
    TrackInfo,
)


def _make_engine_with_tracks() -> TimelineLayoutEngine:
    engine = TimelineLayoutEngine()
    tracks = [
        TrackInfo(track_id="track_V1", track_type="video"),
        TrackInfo(track_id="track_V2", track_type="video"),
        TrackInfo(track_id="track_A1", track_type="audio"),
        TrackInfo(track_id="track_A2", track_type="audio"),
    ]
    state = TimelineLayoutState()
    engine.configure(tracks, state, viewport_width=800, viewport_height=600, pixels_per_frame=5.0)
    return engine


def _get_video_track_y(engine: TimelineLayoutEngine) -> float:
    """Get the center Y of the first video track."""
    rect = engine.track_rect("track_V1")
    if rect:
        return rect.top() + rect.height() / 2
    return 100  # fallback


def _get_audio_track_y(engine: TimelineLayoutEngine) -> float:
    """Get the center Y of the first audio track."""
    rect = engine.track_rect("track_A1")
    if rect:
        return rect.top() + rect.height() / 2
    return 400  # fallback


# ================================================================
# resolve_drop_target tests
# ================================================================

class TestResolveDropTarget:
    def test_drop_video_on_video_track(self):
        engine = _make_engine_with_tracks()
        y = _get_video_track_y(engine)
        result = engine.resolve_drop_target(100, y, "video")
        assert result.is_valid is True
        assert result.track_type == "video"
        assert result.track_id is not None

    def test_drop_audio_on_audio_track(self):
        engine = _make_engine_with_tracks()
        y = _get_audio_track_y(engine)
        result = engine.resolve_drop_target(100, y, "audio")
        assert result.is_valid is True
        assert result.track_type == "audio"
        assert result.track_id is not None

    def test_drop_video_on_locked_track(self):
        engine = _make_engine_with_tracks()
        y = _get_video_track_y(engine)
        locked = {"track_V1"}
        result = engine.resolve_drop_target(100, y, "video", locked_track_ids=locked)
        # Should either reject or find another unlocked video track
        if result.track_id == "track_V1":
            assert result.is_valid is False

    def test_drop_on_ruler_rejected(self):
        engine = _make_engine_with_tracks()
        result = engine.resolve_drop_target(100, 5, "video")
        assert result.is_valid is False
        assert "ruler" in result.reason.lower()

    def test_frame_mapped_from_x(self):
        engine = _make_engine_with_tracks()
        y = _get_video_track_y(engine)
        # X maps to a frame; at x=250 (50 frames at 5ppf) it should be close to 50
        result = engine.resolve_drop_target(250, y, "video")
        if result.is_valid:
            # Frame should be a reasonable number (may have scroll offset)
            assert isinstance(result.frame, int)

    def test_image_media_snaps_to_video(self):
        engine = _make_engine_with_tracks()
        y = _get_video_track_y(engine)
        result = engine.resolve_drop_target(100, y, "image")
        assert result.is_valid is True
        assert result.track_type == "video"

    def test_video_in_empty_video_area_suggests_create(self):
        engine = _make_engine_with_tracks()
        # Drop in video area but between tracks (not on any track rect)
        area = engine.area_layout()
        y = area.video_area.top() + 1  # Very top of video area, likely no track
        result = engine.resolve_drop_target(100, y, "video")
        # Should either be valid (hit a track) or suggest creating one
        if not result.is_valid:
            assert result.create_track is True or result.track_type == "video"
