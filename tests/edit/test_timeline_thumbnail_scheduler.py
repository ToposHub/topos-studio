"""
Tests for timeline_thumbnail_scheduler — viewport-aware thumbnail scheduling.

Validates:
- Viewport-level job computation
- Playback pause behavior
- Filmstrip density based on clip width
- Generation invalidation on scroll/zoom
- Visible source frame sampling
"""

import pytest


# ===================================================================
# Test: Scheduler creation
# ===================================================================

class TestSchedulerCreation:
    """TimelineThumbnailScheduler can be created and configured."""

    def test_default_creation(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        assert scheduler._playback_active is False
        assert len(scheduler._pending_jobs) == 0

    def test_set_playback_active(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        scheduler.set_playback_active(True)
        assert scheduler._playback_active is True


# ===================================================================
# Test: Viewport job computation
# ===================================================================

class TestViewportJobs:
    """update_viewport computes correct thumbnail jobs."""

    def _make_scheduler(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        return TimelineThumbnailScheduler()

    def test_no_jobs_during_playback(self):
        """During playback, update_viewport returns empty list."""
        scheduler = self._make_scheduler()
        scheduler.set_playback_active(True)
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 500}],
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        assert jobs == []

    def test_single_mode_for_narrow_clip(self):
        """Very narrow clips get single thumbnail."""
        scheduler = self._make_scheduler()
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 30, "mode": "filmstrip"}],
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        assert len(jobs) >= 1
        assert jobs[0].mode == "single"

    def test_start_end_mode_for_medium_clip(self):
        """Medium clips get start+end or single thumbnails."""
        scheduler = self._make_scheduler()
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 300, "mode": "filmstrip"}],
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        assert len(jobs) >= 1  # At least one thumbnail

    def test_filmstrip_for_wide_clip(self):
        """Wide clips get multiple filmstrip thumbnails."""
        scheduler = self._make_scheduler()
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 1000, "mode": "filmstrip"}],
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        assert len(jobs) >= 3
        for job in jobs:
            assert job.mode == "filmstrip"

    def test_jobs_have_correct_asset_info(self):
        """Jobs preserve asset information from clip info."""
        scheduler = self._make_scheduler()
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "asset-123", "file_path": "/path/to/video.mov",
                           "clip_id": "clip-456",
                           "source_start": 10, "source_duration": 50,
                           "visible_px_start": 0, "visible_px_end": 500, "mode": "filmstrip"}],
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        assert all(j.asset_id == "asset-123" for j in jobs)
        assert all(j.file_path == "/path/to/video.mov" for j in jobs)
        assert all(j.clip_id == "clip-456" for j in jobs)

    def test_no_jobs_for_zero_width_clip(self):
        """Zero-width visible region produces no jobs."""
        scheduler = self._make_scheduler()
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 100, "visible_px_end": 100, "mode": "filmstrip"}],
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        assert jobs == []

    def test_multiple_clips(self):
        """Multiple visible clips each get their own jobs."""
        scheduler = self._make_scheduler()
        clips = [
            {"asset_id": "a1", "file_path": "/v1.mov", "clip_id": "c1",
             "source_start": 0, "source_duration": 50,
             "visible_px_start": 0, "visible_px_end": 200, "mode": "filmstrip"},
            {"asset_id": "a2", "file_path": "/v2.mov", "clip_id": "c2",
             "source_start": 0, "source_duration": 50,
             "visible_px_start": 200, "visible_px_end": 500, "mode": "filmstrip"},
        ]
        jobs = scheduler.update_viewport(
            visible_clips=clips,
            viewport_width=1000,
            pixels_per_frame=10.0,
        )
        clip_ids = {j.clip_id for j in jobs}
        assert "c1" in clip_ids
        assert "c2" in clip_ids


# ===================================================================
# Test: Generation invalidation
# ===================================================================

class TestGenerationInvalidation:
    """Generation tracking invalidates stale jobs."""

    def test_invalidate_clears_pending(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 500}],
            viewport_width=1000, pixels_per_frame=10.0,
        )
        assert len(scheduler._pending_jobs) > 0
        scheduler.invalidate()
        assert len(scheduler._pending_jobs) == 0

    def test_job_validity_check(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import (
            TimelineThumbnailScheduler, TimelineThumbnailJob,
        )
        scheduler = TimelineThumbnailScheduler()
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 500}],
            viewport_width=1000, pixels_per_frame=10.0,
        )
        assert len(jobs) > 0
        # Current jobs are valid
        assert all(scheduler.is_job_valid(j) for j in jobs)

        # After invalidate, old jobs are stale
        scheduler.invalidate()
        assert not any(scheduler.is_job_valid(j) for j in jobs)


# ===================================================================
# Test: Playback pauses new jobs
# ===================================================================

class TestPlaybackPause:
    """During playback, no new thumbnail jobs are created."""

    def test_playback_clears_pending(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 500}],
            viewport_width=1000, pixels_per_frame=10.0,
        )
        assert len(scheduler._pending_jobs) > 0
        scheduler.set_playback_active(True)
        assert len(scheduler._pending_jobs) == 0

    def test_playback_prevents_new_jobs(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        scheduler.set_playback_active(True)
        jobs = scheduler.update_viewport(
            visible_clips=[{"asset_id": "a1", "file_path": "/test.mov", "clip_id": "c1",
                           "source_start": 0, "source_duration": 100,
                           "visible_px_start": 0, "visible_px_end": 500}],
            viewport_width=1000, pixels_per_frame=10.0,
        )
        assert jobs == []


# ===================================================================
# Test: Visible source frame sampling
# ===================================================================

class TestVisibleSourceFrameSampling:
    """compute_visible_source_frames samples only visible portion."""

    def test_full_clip_visible(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import compute_visible_source_frames
        frames = compute_visible_source_frames(
            source_start=0, source_duration=100,
            visible_px_start=0, visible_px_end=1000,
            clip_total_px=1000, max_thumbnails=5,
        )
        assert len(frames) == 5
        assert frames[0] == 0
        assert frames[-1] == 99

    def test_partial_clip_visible(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import compute_visible_source_frames
        frames = compute_visible_source_frames(
            source_start=0, source_duration=100,
            visible_px_start=500, visible_px_end=1000,
            clip_total_px=1000, max_thumbnails=5,
        )
        # Should only sample from the right half of the clip
        assert len(frames) >= 1
        assert frames[0] >= 40  # Roughly past middle

    def test_single_frame_duration(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import compute_visible_source_frames
        frames = compute_visible_source_frames(
            source_start=0, source_duration=1,
            visible_px_start=0, visible_px_end=500,
            clip_total_px=500, max_thumbnails=5,
        )
        # Single frame duration: all sampled frames are frame 0
        assert all(f == 0 for f in frames)

    def test_zero_duration_returns_empty(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import compute_visible_source_frames
        frames = compute_visible_source_frames(
            source_start=0, source_duration=0,
            visible_px_start=0, visible_px_end=500,
            clip_total_px=500, max_thumbnails=5,
        )
        assert frames == []


# ===================================================================
# Test: Cache
# ===================================================================

class TestThumbnailCache:
    """Thumbnail cache get/set."""

    def test_cache_miss_returns_none(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        assert scheduler.get_cached_thumbnail("a1", 0, 64) is None

    def test_cache_hit_returns_value(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        scheduler.cache_thumbnail("a1", 0, 64, "pixmap_data")
        assert scheduler.get_cached_thumbnail("a1", 0, 64) == "pixmap_data"

    def test_clear_cache(self):
        from src.features.edit.core.timeline_thumbnail_scheduler import TimelineThumbnailScheduler
        scheduler = TimelineThumbnailScheduler()
        scheduler.cache_thumbnail("a1", 0, 64, "data")
        scheduler.clear_cache()
        assert scheduler.get_cached_thumbnail("a1", 0, 64) is None
