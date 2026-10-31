"""Viewport-level thumbnail scheduler for timeline clips.

This module provides the :class:`TimelineThumbnailScheduler` which manages
thumbnail generation based on viewport visibility, zoom level, and playback
state.  It is designed to keep the timeline responsive by:

* **Viewport-aware** -- only generating thumbnails for clips that are
  currently visible on screen.
* **Playback-aware** -- pausing new decode jobs while playback is active so
  that CPU / GPU resources are reserved for real-time rendering.
* **Zoom-aware** -- adjusting the density of filmstrip frames according to
  the pixel width of each clip on the timeline.
* **Batch submission** -- grouping jobs by asset so that the downstream
  decoder can open a file once and decode several frames in sequence.
* **Generation tracking** -- every call to :meth:`update_viewport` increments
  an internal generation counter; stale jobs from earlier generations are
  silently discarded via :meth:`is_job_valid`.

Typical usage
-------------
1. Instantiate ``TimelineThumbnailScheduler``.
2. Call :meth:`set_batch_callback` with a function that receives a list of
   :class:`TimelineThumbnailJob` objects and dispatches them to your
   favourite decode worker / thread-pool.
3. On every viewport change (scroll, zoom, clip move) call
   :meth:`update_viewport` with the current list of visible clips.
4. When the decode worker finishes a job, call :meth:`cache_thumbnail` to
   store the result and check :meth:`is_job_valid` to discard stale results.
5. Before playback starts call :meth:`set_playback_active(True)`;
   after it stops call :meth:`set_playback_active(False)`.

The helper function :func:`compute_visible_source_frames` is also available
for code that needs the raw list of source frames without going through the
full scheduler.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THUMBNAIL_MAX_PER_VISIBLE_CLIP = 20  # Max filmstrip frames per visible clip
THUMBNAIL_DEFAULT_HEIGHT = 64
# AUD-19008：已完成缩略图缓存的数量上限（LRU）。长会话多资产胶片按
# (资产, 帧, 高度) 只增不减，64px 高的 pixmap 持续累积构成内存增长风险；
# 上限参考单轨可见密度（每 clip ≤20 帧）× 常驻多资产规模取整。
THUMBNAIL_CACHE_MAX_ENTRIES = 2048

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineThumbnailJob:
    """A single thumbnail generation job.

    Attributes:
        asset_id: Unique identifier of the source asset.
        file_path: Absolute path to the source media file.
        clip_id: Unique identifier of the timeline clip.
        source_frame: Frame index within the source media to decode.
        target_height: Desired thumbnail height in pixels.
        mode: Sampling mode -- ``"single"``, ``"start_end"``, or ``"filmstrip"``.
        priority: Lower numbers mean higher priority (decoded first).
        generation: The scheduler generation this job belongs to; stale jobs
            should be discarded.
    """

    asset_id: str
    file_path: str
    clip_id: str
    source_frame: int
    target_height: int = THUMBNAIL_DEFAULT_HEIGHT
    mode: str = "single"  # single, start_end, filmstrip
    priority: int = 0  # Lower = higher priority
    generation: int = 0  # Invalidation counter


@dataclass
class ThumbnailBatch:
    """A batch of thumbnail jobs for the current viewport.

    Attributes:
        jobs: Ordered list of :class:`TimelineThumbnailJob` instances.
        generation: The scheduler generation this batch was created in.
        viewport_hash: An opaque hash or string identifying the viewport
            configuration that produced this batch.
    """

    jobs: list[TimelineThumbnailJob] = field(default_factory=list)
    generation: int = 0
    viewport_hash: str = ""


# ---------------------------------------------------------------------------
# Sampling helper
# ---------------------------------------------------------------------------


def compute_visible_source_frames(
    source_start: int,
    source_duration: int,
    visible_px_start: int,
    visible_px_end: int,
    clip_total_px: int,
    max_thumbnails: int = THUMBNAIL_MAX_PER_VISIBLE_CLIP,
) -> list[int]:
    """Compute source frames to sample for a visible clip region.

    Only samples frames that are actually visible, not the entire clip.

    Args:
        source_start: First source frame of the clip.
        source_duration: Total source frames in the clip.
        visible_px_start: Start of visible region in viewport pixels.
        visible_px_end: End of visible region in viewport pixels.
        clip_total_px: Total pixel width of the clip in the timeline.
        max_thumbnails: Maximum number of thumbnails to generate.

    Returns:
        Sorted list of source frame indices to sample.
    """
    if clip_total_px <= 0 or source_duration <= 0:
        return [source_start] if source_duration > 0 else []

    # Map visible pixel range to source frame range
    vis_start_frac = visible_px_start / clip_total_px
    vis_end_frac = visible_px_end / clip_total_px

    source_vis_start = source_start + int(vis_start_frac * source_duration)
    source_vis_end = source_start + int(vis_end_frac * source_duration)

    visible_count = max(1, source_vis_end - source_vis_start)

    # Determine how many thumbnails fit
    thumb_w = max(48, THUMBNAIL_DEFAULT_HEIGHT * 16 // 9)
    visible_px = visible_px_end - visible_px_start
    count = min(max_thumbnails, max(1, visible_px // thumb_w))

    if count <= 1:
        return [source_vis_start]

    # Uniform sampling in visible range
    frames: list[int] = []
    for i in range(count):
        t = i / (count - 1)
        frame = source_vis_start + int(t * (visible_count - 1))
        frame = max(source_start, min(frame, source_start + source_duration - 1))
        frames.append(frame)

    return frames


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TimelineThumbnailScheduler:
    """Viewport-level scheduler for timeline clip thumbnails.

    Manages thumbnail generation based on viewport visibility, zoom level,
    and playback state.  Prioritises visible clips and pauses new jobs
    during playback.

    Features:
    - **Viewport-aware**: only generates thumbnails for visible clips.
    - **Playback-aware**: pauses new decode jobs during playback.
    - **Zoom-aware**: adjusts filmstrip density based on clip width.
    - **Batch submission**: groups jobs by asset for efficient decoding.
    - **Generation tracking**: invalidates stale requests on scroll / zoom.
    """

    def __init__(self) -> None:
        self._generation: int = 0
        self._playback_active: bool = False
        self._pending_jobs: dict[str, TimelineThumbnailJob] = {}  # clip_id -> job
        # AUD-19008：dict → OrderedDict LRU（move_to_end + 超限逐出最旧），
        # 缓存不再无界增长。解码完成回调可能经 Qt 信号落到非调用线程，
        # 读写加锁保护（同目录 timeline_render_plan 的缓存模式）。
        self._completed_cache: OrderedDict[tuple[str, int, int], object] = OrderedDict()
        self._cache_lock: threading.Lock = threading.Lock()
        self._on_batch_callback: Callable[[list[TimelineThumbnailJob]], None] | None = None
        self._debounce_pending: bool = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_batch_callback(self, callback: Callable[[list[TimelineThumbnailJob]], None]) -> None:
        """Set callback for when a batch of jobs is ready for processing."""
        self._on_batch_callback = callback

    def set_playback_active(self, active: bool) -> None:
        """Set whether playback is active.

        During playback, no new thumbnail decode jobs are submitted.
        Only cached thumbnails are displayed.
        """
        self._playback_active = active
        if active:
            self._pending_jobs.clear()

    # ------------------------------------------------------------------
    # Viewport
    # ------------------------------------------------------------------

    def update_viewport(
        self,
        visible_clips: list[dict],
        viewport_width: int,
        pixels_per_frame: float,
    ) -> list[TimelineThumbnailJob]:
        """Compute thumbnail jobs for the current viewport.

        Args:
            visible_clips: List of dicts with keys:
                - ``asset_id``, ``file_path``, ``clip_id``
                - ``source_start``, ``source_duration``
                - ``visible_px_start``, ``visible_px_end`` (pixel range in viewport)
            viewport_width: Viewport width in pixels.
            pixels_per_frame: How many pixels one timeline frame occupies.

        Returns:
            List of :class:`TimelineThumbnailJob` to submit (empty if
            playback is active).
        """
        if self._playback_active:
            return []

        self._generation += 1
        jobs: list[TimelineThumbnailJob] = []

        for clip_info in visible_clips:
            clip_jobs = self._compute_clip_jobs(clip_info, pixels_per_frame)
            jobs.extend(clip_jobs)

        # Sort by priority (lower = higher priority)
        jobs.sort(key=lambda j: j.priority)

        # Store pending
        self._pending_jobs = {j.clip_id: j for j in jobs}

        return jobs

    # ------------------------------------------------------------------
    # Job computation
    # ------------------------------------------------------------------

    def _compute_clip_jobs(self, clip_info: dict, pixels_per_frame: float) -> list[TimelineThumbnailJob]:
        """Compute thumbnail jobs for a single visible clip."""
        asset_id: str = clip_info.get("asset_id", "")
        file_path: str = clip_info.get("file_path", "")
        clip_id: str = clip_info.get("clip_id", "")
        source_start: int = clip_info.get("source_start", 0)
        source_duration: int = clip_info.get("source_duration", 1)
        visible_px_start: int = clip_info.get("visible_px_start", 0)
        visible_px_end: int = clip_info.get("visible_px_end", 0)
        mode: str = clip_info.get("mode", "filmstrip")

        visible_width = visible_px_end - visible_px_start
        if visible_width <= 0:
            return []

        aspect_ratio = 16.0 / 9.0
        thumb_w = max(48, THUMBNAIL_DEFAULT_HEIGHT * aspect_ratio)

        if mode == "single" or visible_width < thumb_w * 2:
            # Too small for filmstrip - just start frame
            return [TimelineThumbnailJob(
                asset_id=asset_id,
                file_path=file_path,
                clip_id=clip_id,
                source_frame=source_start,
                priority=0,
                mode="single",
                generation=self._generation,
            )]

        if mode == "start_end" or visible_width < thumb_w * 4:
            # Start and end frames
            return [
                TimelineThumbnailJob(
                    asset_id=asset_id,
                    file_path=file_path,
                    clip_id=clip_id,
                    source_frame=source_start,
                    priority=0,
                    mode="start_end",
                    generation=self._generation,
                ),
                TimelineThumbnailJob(
                    asset_id=asset_id,
                    file_path=file_path,
                    clip_id=clip_id,
                    source_frame=source_start + source_duration - 1,
                    priority=1,
                    mode="start_end",
                    generation=self._generation,
                ),
            ]

        # Filmstrip: uniform sample based on visible width
        max_count = min(
            THUMBNAIL_MAX_PER_VISIBLE_CLIP,
            max(1, int(visible_width / thumb_w)),
        )

        jobs: list[TimelineThumbnailJob] = []
        for i in range(max_count):
            # Uniform sampling in source range
            if max_count > 1:
                t = i / (max_count - 1)
            else:
                t = 0.5
            frame = source_start + int(t * (source_duration - 1))

            # Priority: first/last frames higher than middle
            if i == 0 or i == max_count - 1:
                priority = 0
            elif i < max_count // 3:
                priority = 1
            else:
                priority = 2

            jobs.append(TimelineThumbnailJob(
                asset_id=asset_id,
                file_path=file_path,
                clip_id=clip_id,
                source_frame=frame,
                priority=priority,
                mode="filmstrip",
                generation=self._generation,
            ))

        return jobs

    # ------------------------------------------------------------------
    # Invalidation / generation
    # ------------------------------------------------------------------

    def invalidate(self) -> None:
        """Invalidate all pending jobs (e.g. on zoom / scroll change)."""
        self._generation += 1
        self._pending_jobs.clear()

    def is_job_valid(self, job: TimelineThumbnailJob) -> bool:
        """Check if a job is still valid (not stale)."""
        return job.generation == self._generation

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def cache_thumbnail(self, asset_id: str, source_frame: int, height: int, pixmap: object) -> None:
        """Cache a completed thumbnail (LRU; oldest entries evicted past the cap)."""
        key = (asset_id, source_frame, height)
        with self._cache_lock:
            self._completed_cache[key] = pixmap
            self._completed_cache.move_to_end(key)
            while len(self._completed_cache) > THUMBNAIL_CACHE_MAX_ENTRIES:
                self._completed_cache.popitem(last=False)

    def get_cached_thumbnail(self, asset_id: str, source_frame: int, height: int) -> object | None:
        """Get a cached thumbnail, or ``None``. Touches the LRU order."""
        key = (asset_id, source_frame, height)
        with self._cache_lock:
            pixmap = self._completed_cache.get(key)
            if pixmap is not None:
                self._completed_cache.move_to_end(key)
            return pixmap

    def clear_cache(self) -> None:
        """Clear the thumbnail cache."""
        with self._cache_lock:
            self._completed_cache.clear()
