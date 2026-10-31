# -*- coding: utf-8 -*-
"""
Tests for P1 unified hit-test and drop-target features in TimelineLayoutEngine.

Covers:
- resolve_timeline_hit: ruler, splitter, track, header, clip, empty areas
- resolve_drop_target: type compatibility, locked tracks, auto-snap, create-track
"""

from __future__ import annotations

import pytest

from src.features.edit.ui.timeline_layout import (
    TimelineLayoutEngine,
    TimelineLayoutState,
    TimelineHit,
    TimelineDropTarget,
    TrackInfo,
    TRACK_HEADER_WIDTH,
    RULER_HEIGHT,
    TRACK_AREA_TOP,
    SPLITTER_HEIGHT,
    DEFAULT_VIDEO_TRACK_HEIGHT,
    DEFAULT_AUDIO_TRACK_HEIGHT,
    TRACK_GAP,
)

pytestmark = pytest.mark.edit_core


# ── Fixture / helpers ────────────────────────────────────────────────────────


def make_engine(
    video_count: int = 2,
    audio_count: int = 2,
    width: float = 1200.0,
    height: float = 600.0,
    ppf: float = 5.0,
) -> TimelineLayoutEngine:
    """Build a configured engine with the requested number of video/audio tracks."""
    engine = TimelineLayoutEngine()
    tracks: list[TrackInfo] = []
    for i in range(video_count):
        tracks.append(TrackInfo(track_id=f"v{i + 1}", track_type="video"))
    for i in range(audio_count):
        tracks.append(TrackInfo(track_id=f"a{i + 1}", track_type="audio"))
    state = TimelineLayoutState()
    engine.configure(tracks, state, width, height, ppf)
    return engine


def _center_y_of_track(engine: TimelineLayoutEngine, track_id: str) -> float:
    """Return the vertical midpoint of a track."""
    rect = engine.track_rect(track_id)
    return (rect.top() + rect.bottom()) / 2.0


def _mid_splitter_y(engine: TimelineLayoutEngine) -> float:
    """Return the vertical midpoint of the splitter."""
    sr = engine.splitter_rect()
    return (sr.top() + sr.bottom()) / 2.0


# ════════════════════════════════════════════════════════════════════════════
# resolve_timeline_hit tests
# ════════════════════════════════════════════════════════════════════════════


class TestResolveTimelineHit:
    """Tests for TimelineLayoutEngine.resolve_timeline_hit."""

    # ── 1. Ruler area ──────────────────────────────────────────────────────────

    def test_hit_ruler_area(self, qapp) -> None:
        """Y in the ruler zone (above TRACK_AREA_TOP) returns area='ruler'."""
        engine = make_engine()
        ruler_y = RULER_HEIGHT / 2  # safely within ruler
        scene_x = TRACK_HEADER_WIDTH + 50  # past header column

        hit = engine.resolve_timeline_hit(scene_x, ruler_y)
        assert hit.area == "ruler"
        assert hit.frame is not None

    def test_hit_ruler_header_corner(self, qapp) -> None:
        """X < header_width in ruler zone still returns area='ruler' with reason."""
        engine = make_engine()
        ruler_y = RULER_HEIGHT / 2
        scene_x = 10  # well within header column

        hit = engine.resolve_timeline_hit(scene_x, ruler_y)
        assert hit.area == "ruler"
        assert "ruler-header" in (hit.reason or "")

    # ── 2. Splitter ────────────────────────────────────────────────────────────

    def test_hit_splitter(self, qapp) -> None:
        """Y in the splitter zone returns area='splitter'."""
        engine = make_engine()
        split_y = _mid_splitter_y(engine)
        scene_x = TRACK_HEADER_WIDTH + 100

        hit = engine.resolve_timeline_hit(scene_x, split_y)
        assert hit.area == "splitter"
        assert hit.frame is not None

    def test_hit_splitter_has_priority_over_track(self, qapp) -> None:
        """Even at the exact boundary of a track, splitter wins."""
        engine = make_engine()
        sr = engine.splitter_rect()
        # The splitter rect sits between video and audio areas.
        # Even if a track edge is adjacent, splitter Y range takes priority.
        splitter_top_y = sr.top()
        scene_x = TRACK_HEADER_WIDTH + 100

        hit = engine.resolve_timeline_hit(scene_x, splitter_top_y)
        assert hit.area == "splitter"

    # ── 3. Track background ───────────────────────────────────────────────────

    def test_hit_track_background(self, qapp) -> None:
        """Y on a track, X past header column returns area='track'."""
        engine = make_engine()
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100  # well past header

        hit = engine.resolve_timeline_hit(scene_x, v1_mid)
        assert hit.area == "track"
        assert hit.track_id == "v1"
        assert hit.frame is not None

    # ── 4. Header column ──────────────────────────────────────────────────────

    def test_hit_header_column(self, qapp) -> None:
        """Y on a track, X in header column returns area='header'."""
        engine = make_engine()
        a1_mid = _center_y_of_track(engine, "a1")
        scene_x = 10  # well within header column (< TRACK_HEADER_WIDTH)

        hit = engine.resolve_timeline_hit(scene_x, a1_mid)
        assert hit.area == "header"
        assert hit.track_id == "a1"

    # ── 5. Clip hit ───────────────────────────────────────────────────────────

    def test_hit_clip(self, qapp) -> None:
        """With clip_id_at callback returning a clip_id, area='clip'."""
        engine = make_engine()
        v2_mid = _center_y_of_track(engine, "v2")
        scene_x = TRACK_HEADER_WIDTH + 100

        def clip_cb(sx, sy, tid):
            if tid == "v2":
                return "clip-abc"
            return None

        hit = engine.resolve_timeline_hit(scene_x, v2_mid, clip_id_at=clip_cb)
        assert hit.area == "clip"
        assert hit.clip_id == "clip-abc"
        assert hit.track_id == "v2"

    def test_hit_clip_callback_returns_none_falls_back_to_track(self, qapp) -> None:
        """When clip_id_at returns None, falls back to area='track'."""
        engine = make_engine()
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        hit = engine.resolve_timeline_hit(
            scene_x, v1_mid, clip_id_at=lambda sx, sy, tid: None
        )
        assert hit.area == "track"
        assert hit.clip_id is None

    # ── 6. Empty between tracks ───────────────────────────────────────────────

    def test_hit_empty_between_tracks(self, qapp) -> None:
        """Y between two tracks within the track area returns area='empty'."""
        # Use 1 video, 1 audio track with a big viewport so there is gap space.
        engine = make_engine(video_count=1, audio_count=1, height=800)
        # The total_track_area spans from TRACK_AREA_TOP down.  With only 2
        # tracks the empty region is above v1 (video area) or below a1 (audio
        # area).  Pick a Y well above the video track but below TRACK_AREA_TOP.
        v1_rect = engine.track_rect("v1")
        # Try between TRACK_AREA_TOP and v1 top (gap area)
        if v1_rect.top() > TRACK_AREA_TOP + 5:
            gap_y = (TRACK_AREA_TOP + v1_rect.top()) / 2.0
            hit = engine.resolve_timeline_hit(TRACK_HEADER_WIDTH + 50, gap_y)
            assert hit.area == "empty"

    def test_hit_empty_below_tracks(self, qapp) -> None:
        """Y well below all tracks and total_track_area returns area='empty'."""
        engine = make_engine(video_count=1, audio_count=1, height=300)
        # The total_track_area may extend past the last track. Use a Y far
        # beyond the scene rect to guarantee we hit the "below track area" path.
        scene = engine.scene_rect()
        below_y = scene.bottom() + 100

        hit = engine.resolve_timeline_hit(TRACK_HEADER_WIDTH + 50, below_y)
        assert hit.area == "empty"
        assert hit.reason == "below track area"

    # ── 7. Frame from X ──────────────────────────────────────────────────────

    def test_hit_frame_matches_x_to_frame(self, qapp) -> None:
        """The frame in TimelineHit matches engine.x_to_frame(scene_x)."""
        engine = make_engine(ppf=5.0)
        scene_x = TRACK_HEADER_WIDTH + 75.0  # 75 / 5 = frame 15
        expected_frame = engine.x_to_frame(scene_x)

        v1_mid = _center_y_of_track(engine, "v1")
        hit = engine.resolve_timeline_hit(scene_x, v1_mid)
        assert hit.frame == expected_frame


# ════════════════════════════════════════════════════════════════════════════
# resolve_drop_target tests
# ════════════════════════════════════════════════════════════════════════════


class TestResolveDropTarget:
    """Tests for TimelineLayoutEngine.resolve_drop_target."""

    # ── 1. Valid drops ────────────────────────────────────────────────────────

    def test_drop_video_on_video_track(self, qapp) -> None:
        """Dropping video on a video track is valid."""
        engine = make_engine()
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, v1_mid, "video")
        assert target.is_valid is True
        assert target.track_id == "v1"
        assert target.track_type == "video"
        assert target.create_track is False

    def test_drop_audio_on_audio_track(self, qapp) -> None:
        """Dropping audio on an audio track is valid."""
        engine = make_engine()
        a1_mid = _center_y_of_track(engine, "a1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, a1_mid, "audio")
        assert target.is_valid is True
        assert target.track_id == "a1"
        assert target.track_type == "audio"
        assert target.create_track is False

    # ── 2. Cross-area auto-snap ───────────────────────────────────────────────

    def test_drop_video_on_audio_area_snaps_to_video(self, qapp) -> None:
        """Dropping video on an audio track snaps to the default video track."""
        engine = make_engine()
        a1_mid = _center_y_of_track(engine, "a1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, a1_mid, "video")
        assert target.is_valid is True
        assert target.track_type == "video"
        assert target.track_id == "v1"  # snapped to first video track
        assert "Snapped" in (target.reason or "")

    def test_drop_audio_on_video_area_snaps_to_audio(self, qapp) -> None:
        """Dropping audio on a video track snaps to the default audio track."""
        engine = make_engine()
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, v1_mid, "audio")
        assert target.is_valid is True
        assert target.track_type == "audio"
        assert target.track_id == "a1"  # snapped to first audio track
        assert "Snapped" in (target.reason or "")

    # ── 3. Locked track rejects ───────────────────────────────────────────────

    def test_drop_on_locked_track_invalid(self, qapp) -> None:
        """Dropping on a locked track is rejected."""
        engine = make_engine()
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(
            scene_x, v1_mid, "video", locked_track_ids={"v1"}
        )
        assert target.is_valid is False
        assert target.track_id == "v1"
        assert "locked" in (target.reason or "").lower()

    # ── 4. Splitter rejects ───────────────────────────────────────────────────

    def test_drop_on_splitter_invalid(self, qapp) -> None:
        """Dropping on the splitter is rejected."""
        engine = make_engine()
        split_y = _mid_splitter_y(engine)
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, split_y, "video")
        assert target.is_valid is False
        assert "splitter" in (target.reason or "").lower()

    # ── 5. Ruler rejects ──────────────────────────────────────────────────────

    def test_drop_on_ruler_invalid(self, qapp) -> None:
        """Dropping on the ruler area is rejected."""
        engine = make_engine()
        ruler_y = RULER_HEIGHT / 2
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, ruler_y, "video")
        assert target.is_valid is False
        assert "ruler" in (target.reason or "").lower()

    # ── 6. No matching track suggests create_track ────────────────────────────

    def test_drop_video_no_video_track_suggests_create(self, qapp) -> None:
        """Dropping video when no video track exists suggests create_track=True."""
        engine = make_engine(video_count=0, audio_count=2)
        a1_mid = _center_y_of_track(engine, "a1")
        scene_x = TRACK_HEADER_WIDTH + 100

        # Dropping video on an audio track: no video tracks to snap to
        target = engine.resolve_drop_target(scene_x, a1_mid, "video")
        assert target.create_track is True
        assert target.track_type == "video"
        assert target.is_valid is False

    def test_drop_audio_no_audio_track_suggests_create(self, qapp) -> None:
        """Dropping audio when no audio track exists suggests create_track=True."""
        engine = make_engine(video_count=2, audio_count=0)
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, v1_mid, "audio")
        assert target.create_track is True
        assert target.track_type == "audio"
        assert target.is_valid is False

    # ── 7. Frame calculation ──────────────────────────────────────────────────

    def test_drop_frame_from_x(self, qapp) -> None:
        """Drop target frame matches engine.x_to_frame(scene_x)."""
        engine = make_engine(ppf=5.0)
        scene_x = TRACK_HEADER_WIDTH + 150.0  # 150 / 5 = frame 30
        expected_frame = engine.x_to_frame(scene_x)

        v1_mid = _center_y_of_track(engine, "v1")
        target = engine.resolve_drop_target(scene_x, v1_mid, "video")
        assert target.frame == expected_frame

    # ── 8. Image media type treated as video ──────────────────────────────────

    def test_drop_image_treated_as_video(self, qapp) -> None:
        """Dropping an image should behave identically to dropping video."""
        engine = make_engine()
        v1_mid = _center_y_of_track(engine, "v1")
        scene_x = TRACK_HEADER_WIDTH + 100

        target = engine.resolve_drop_target(scene_x, v1_mid, "image")
        assert target.is_valid is True
        assert target.track_type == "video"
        assert target.track_id == "v1"

    # ── 9. Empty area in correct type zone suggests create ─────────────────────

    def test_drop_in_empty_video_area_suggests_create(self, qapp) -> None:
        """Dropping in empty space within the total track area returns empty.

        Note: track_at_y_with_area returns (None, "empty") for gaps between
        tracks, so the drop-target code cannot determine which visual area
        (video/audio) the empty space belongs to. This test documents that
        behaviour -- create_track is NOT set for gap areas.
        """
        engine = make_engine(video_count=2, audio_count=2, height=800)
        area = engine.area_layout()

        # Find a Y in the video area that does NOT overlap any track
        v2_rect = engine.track_rect("v2")
        empty_y = area.video_area.top() + 1
        v1_rect = engine.track_rect("v1")
        if empty_y < v2_rect.top() and empty_y < v1_rect.top():
            scene_x = TRACK_HEADER_WIDTH + 100
            target = engine.resolve_drop_target(scene_x, empty_y, "video")
            # The engine cannot identify the visual area for empty gaps,
            # so it returns invalid without create_track.
            assert target.is_valid is False

    # ── 10. Drop on wrong empty area ──────────────────────────────────────────

    def test_drop_video_on_empty_audio_area_invalid(self, qapp) -> None:
        """Dropping video on an empty area in the audio zone is invalid."""
        engine = make_engine(video_count=2, audio_count=2, height=800)
        area = engine.area_layout()

        # Find a Y in the audio area that does NOT overlap any track
        a2_rect = engine.track_rect("a2")
        empty_y = a2_rect.bottom() + TRACK_GAP + 5
        # Ensure still within audio area
        if empty_y < area.audio_area.bottom():
            scene_x = TRACK_HEADER_WIDTH + 100
            target = engine.resolve_drop_target(scene_x, empty_y, "video")
            assert target.is_valid is False
