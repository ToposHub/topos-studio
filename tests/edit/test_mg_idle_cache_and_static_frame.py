"""Motion Graphic idle cache rendering + playback static-frame fallback.

Playback must never wait for (or be blocked by) an unrendered Motion Graphic
clip:

* Space starts the transport immediately — the old auto-cache gate in
  ``TimelinePanel.toggle_playback`` is gone.
* While a clip's disk render cache is not VALID, playback shows a frozen
  frame from the memory scene cache; if none exists yet, one is rendered on
  the background teaching-render thread instead of blocking the preview
  worker on a browser capture.
* The Edit controller renders missing disk caches while the session is idle
  and cancels those background renders the moment playback starts.

The tests exercise the decision points (which path is taken, what gets
cancelled/scheduled) rather than final pixels, mirroring the style of
``test_adjustment_clip_cpu_export_application.py``.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Headless Qt — the scheduler host is a QObject (QTimer parent).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from PyQt6.QtCore import QObject

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.render_cache_status import STATUS_UNCACHED
from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.core.model import (
    SCENE_SCHEMA_VERSION, TeachingSceneDocument,
)
from src.features.teaching_animation.render.cache import (
    RenderCacheManifest, SceneRenderCache,
)
from src.features.teaching_animation.render.frame_adapter import RgbaFrame

pytestmark = pytest.mark.edit_core


def _tiny_scene(scene_id: str = "s1", duration: int = 10) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id=scene_id, schema_version=SCENE_SCHEMA_VERSION,
        name=f"scene {scene_id}", width=8, height=8,
        fps_num=24, fps_den=1, duration_frames=duration,
        background={"color": "#000000"}, elements=(), animations=(),
    )


def _rendered(tag: str, w: int = 8, h: int = 8) -> RenderedFrame:
    return RenderedFrame(
        frame=0, width=w, height=h, pixel_format="rgba8",
        data=bytes([ord(tag)]) * (w * h * 4), backend="html",
    )


def _manifest(scene_id: str, frame: int, *, build: str = "build-confirmed") -> RenderCacheManifest:
    return RenderCacheManifest(
        scene_id=scene_id, scene_hash="hash", renderer_version="v",
        backend="html", frame=frame, width=8, height=8, renderer_build=build,
    )


def _mg_layer(source_frame: int = 5, scene_id: str = "s1"):
    layer = MagicMock()
    layer.asset_id = f"teaching_animation:{scene_id}"
    layer.clip_id = "clip-1"
    layer.source_frame = source_frame
    return layer


# ── SceneRenderCache.nearest_frame_for_scene ─────────────────────────

class TestNearestFrameForScene:
    def test_empty_cache_returns_none(self) -> None:
        assert SceneRenderCache().nearest_frame_for_scene("s1", 0) is None

    def test_returns_closest_frame_number_and_frame(self) -> None:
        cache = SceneRenderCache()
        for frame in (0, 10, 20):
            cache.put(_manifest("s1", frame), _rendered(str(frame % 10)))
        frame_no, rendered = cache.nearest_frame_for_scene("s1", 6)
        assert frame_no == 10
        assert rendered.data[0] == ord("0")  # tag "0" belongs to frame 10

    def test_clamps_to_extremes_and_filters_scenes(self) -> None:
        cache = SceneRenderCache()
        for frame in (0, 10, 20):
            cache.put(_manifest("s1", frame), _rendered("1"))
        cache.put(_manifest("s2", 7), _rendered("2"))
        assert cache.nearest_frame_for_scene("s1", 99)[0] == 20
        assert cache.nearest_frame_for_scene("s2", 0)[0] == 7
        assert cache.nearest_frame_for_scene("s3", 5) is None


# ── schedule_teaching_scene_frame dedup ──────────────────────────────

class TestScheduleTeachingSceneFrame:
    def test_missing_scene_returns_false(self) -> None:
        from src.features.teaching_animation.host_adapters import (
            schedule_teaching_scene_frame,
        )
        assert schedule_teaching_scene_frame("missing", 0) is False

    def test_dedupes_while_job_in_flight(self) -> None:
        from src.features.teaching_animation import host_adapters as ha

        scene = _tiny_scene()
        coordinator = MagicMock()
        handle = MagicMock()
        handle.done = False
        coordinator.worker.submit.return_value = handle
        try:
            with patch(
                "src.features.teaching_animation.scene_cache.get_shared_scene_cache_coordinator",
                return_value=coordinator,
            ):
                first = ha.schedule_teaching_scene_frame(scene.scene_id, 3, scenes={scene.scene_id: scene})
                second = ha.schedule_teaching_scene_frame(scene.scene_id, 4, scenes={scene.scene_id: scene})
            assert first is True
            assert second is False
            assert coordinator.worker.submit.call_count == 1
        finally:
            ha.reset_teaching_scene_frame_schedule(scene.scene_id)


# ── Playback static-frame fallback in PreviewRenderWorker ────────────

def _bare_worker(decode_target=(8, 8)):
    """A PreviewRenderWorker with only the state _get_layer_frame touches."""
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    # ``_decode_target_size`` is a computed property; the per-task active
    # size is its authoritative input and avoids the resolution fallback.
    worker._active_decode_target_size = decode_target
    worker._playback_active = False
    worker._mg_static_frames = {}
    worker._mg_static_frames_max = 32
    return worker


class TestMotionGraphicStaticFrameFallback:
    def _patched_cache(self, cache: SceneRenderCache):
        coordinator = SimpleNamespace(cache=cache)
        return patch(
            "src.features.teaching_animation.scene_cache.get_shared_scene_cache_coordinator",
            return_value=coordinator,
        )

    def test_playback_returns_nearest_cached_frame_for_any_request(self) -> None:
        worker = _bare_worker()
        worker._playback_active = True
        worker._get_motion_graphic_render_cache_frame = lambda layer: None
        cache = SceneRenderCache()
        cache.put(_manifest("s1", 2), _rendered("A"))

        with self._patched_cache(cache), patch(
            "src.features.teaching_animation.host_adapters.schedule_teaching_scene_frame",
        ) as schedule:
            frame = worker._get_layer_frame(_mg_layer(source_frame=8))
        assert frame is not None
        assert frame.shape == (8, 8, 4)
        # RGBA "A" (65,65,65,65) → BGR keeps alpha; R↔B swap is a no-op here.
        assert frame[0, 0].tolist() == [65, 65, 65, 65]
        schedule.assert_not_called()

    def test_playback_with_empty_cache_schedules_background_poster(self) -> None:
        worker = _bare_worker()
        worker._playback_active = True
        worker._get_motion_graphic_render_cache_frame = lambda layer: None

        with self._patched_cache(SceneRenderCache()), patch(
            "src.features.teaching_animation.host_adapters.schedule_teaching_scene_frame",
            return_value=True,
        ) as schedule, patch(
            "src.features.teaching_animation.scene_store.get_shared_scene_store",
        ) as store:
            store.return_value.get.return_value = _tiny_scene()
            frame = worker._get_layer_frame(_mg_layer(source_frame=3))
        assert frame is None  # 该层暂时缺席，静帧就绪后自动出现
        schedule.assert_called_once()

    def test_paused_keeps_synchronous_scene_render(self) -> None:
        worker = _bare_worker()
        worker._playback_active = False
        worker._get_motion_graphic_render_cache_frame = lambda layer: None
        rgba = RgbaFrame(
            frame=5, width=8, height=8,
            data=bytes([10, 20, 30, 255]) * (8 * 8),
        )

        with patch(
            "src.features.teaching_animation.host_adapters.render_teaching_scene_frame",
            return_value=rgba,
        ) as render, patch(
            "src.features.teaching_animation.scene_store.get_shared_scene_store",
        ) as store:
            store.return_value.get.return_value = _tiny_scene()
            frame = worker._get_layer_frame(_mg_layer(source_frame=5))
        assert frame is not None
        # BGR conversion: first pixel swaps R(10)↔B(30), alpha preserved.
        assert frame[0, 0].tolist() == [30, 20, 10, 255]
        render.assert_called_once()

    def test_disk_cache_hit_wins_before_static_fallback(self) -> None:
        worker = _bare_worker()
        worker._playback_active = True
        disk_frame = np.full((8, 8, 4), 7, dtype=np.uint8)
        worker._get_motion_graphic_render_cache_frame = lambda layer: disk_frame
        assert worker._get_layer_frame(_mg_layer()) is disk_frame

    def test_set_playback_active_false_clears_static_memo(self) -> None:
        worker = _bare_worker()
        worker._mg_static_frames[("s1", 0, (8, 8))] = np.zeros((8, 8, 4), np.uint8)
        worker.set_playback_active(False)
        assert worker._mg_static_frames == {}
        worker.set_playback_active(True)
        assert worker._playback_active is True


# ── EditControllerMixin idle scheduler ───────────────────────────────

class _SchedulerHost(QObject, EditControllerMixin):
    """Minimal QObject host so the mixin can parent its QTimer."""


def _make_host():
    host = _SchedulerHost()
    clip = MagicMock()
    clip.clip_id = "clip-1"
    clip.asset_id = "teaching_animation:s1"
    track = MagicMock()
    track.track_id = "track-1"
    track.clips = [clip]
    timeline = MagicMock()
    timeline.timeline_id = "t1"
    timeline.all_tracks = [track]
    asset = MagicMock()
    asset.media_type = "motion_graphic"
    project = MagicMock()
    project.assets = {"teaching_animation:s1": asset}

    host.edit_page = MagicMock()
    host.edit_page._current_timeline = timeline
    host.edit_page._is_playing = False
    host.video_controller = MagicMock()
    host.video_controller.is_playing = False
    host.timeline_preview_renderer = MagicMock()
    host.timeline_preview_renderer.is_render_idle = lambda: True
    host._current_edit_project = project
    host.status_bar = MagicMock()
    host._cleanup_obsolete_render_cache_files = MagicMock()
    host._render_cache_workers = {}
    host._render_cache_inflight_clips = set()
    host._render_cache_inflight_cache_keys = {}
    return host


class TestIdleCacheScheduler:
    def test_idle_starts_one_cache_render(self, qapp) -> None:
        host = _make_host()
        requested = []

        def fake_request(clip_id, track_id):
            requested.append((clip_id, track_id))
            worker = MagicMock()
            worker._manifest = SimpleNamespace(render_kind="motion_graphic")
            host._render_cache_workers["cache-new"] = worker
            return True

        host._on_render_cache_clip_requested = fake_request
        try:
            with patch(
                "src.features.edit.core.render_cache_status.cache_status_for_clip",
                return_value=STATUS_UNCACHED,
            ):
                host._ensure_mg_idle_cache_scheduler()
                host._maybe_start_idle_mg_cache()
        finally:
            host._mg_idle_cache_timer.stop()
        assert requested == [("clip-1", "track-1")]
        assert host._mg_idle_cache_ids == {"cache-new"}

    def test_playing_does_not_start(self, qapp) -> None:
        host = _make_host()
        host.edit_page._is_playing = True
        host._on_render_cache_clip_requested = MagicMock()
        host._ensure_mg_idle_cache_scheduler()
        try:
            host._maybe_start_idle_mg_cache()
        finally:
            host._mg_idle_cache_timer.stop()
        host._on_render_cache_clip_requested.assert_not_called()

    def test_existing_mg_worker_blocks_new_one(self, qapp) -> None:
        host = _make_host()
        busy = MagicMock()
        busy._manifest = SimpleNamespace(render_kind="motion_graphic")
        host._render_cache_workers["busy"] = busy
        host._on_render_cache_clip_requested = MagicMock()
        host._ensure_mg_idle_cache_scheduler()
        try:
            host._maybe_start_idle_mg_cache()
        finally:
            host._mg_idle_cache_timer.stop()
        host._on_render_cache_clip_requested.assert_not_called()

    def test_playback_start_cancels_idle_render_and_invalidates(self, qapp) -> None:
        host = _make_host()
        worker = MagicMock()
        worker._manifest = SimpleNamespace(render_kind="motion_graphic")
        host._render_cache_workers["cache-1"] = worker
        host._mg_idle_cache_ids = {"cache-1"}
        host._render_cache_inflight_cache_keys["cache-1"] = ("t1", "clip-1")
        host._render_cache_inflight_clips.add(("t1", "clip-1"))
        project = host._current_edit_project
        updated_project = MagicMock()
        project.invalidate_render_cache_for_clip.return_value = updated_project
        host.timeline_panel = MagicMock()
        host.media_pool_panel = MagicMock()

        host._on_playback_state_for_mg_idle(True, 1.0, 1)

        worker.cancel.assert_called_once()
        assert host._render_cache_workers == {}
        assert host._mg_idle_cache_ids == set()
        assert ("t1", "clip-1") not in host._render_cache_inflight_clips
        project.invalidate_render_cache_for_clip.assert_called_once_with(
            "t1", "clip-1",
        )
        # 取消后工程快照被替换，panel 与 preview renderer 同步拿到新快照。
        assert host._current_edit_project is updated_project
        host.timeline_panel.set_project.assert_called_once_with(updated_project)


# ── Space gate removal ───────────────────────────────────────────────

class TestTogglePlaybackImmediate:
    def test_space_starts_playback_without_waiting_for_cache(self, qapp) -> None:
        from src.features.edit.ui.timeline_panel import TimelinePanel

        panel = TimelinePanel.__new__(TimelinePanel)
        panel.timeline_view = SimpleNamespace(_timeline=None)
        panel._playback_controller = MagicMock()
        panel._playback_controller.is_playing = False
        panel._playback_controller.current_frame = 0
        panel._fps = 24.0
        panel._total_frames = 100

        panel.toggle_playback()

        panel._playback_controller.play.assert_called_once()
