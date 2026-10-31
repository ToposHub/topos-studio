"""Tests for the playback AUTO_2K/FULL quality policy and queue coalescing."""

import queue
from types import SimpleNamespace

import pytest

from src.features.edit.core.timeline_preview_renderer import (
    PreviewRenderWorker,
    TimelinePreviewRenderer,
)


def _renderer_for_geometry(width=12000, height=6750):
    renderer = TimelinePreviewRenderer.__new__(TimelinePreviewRenderer)
    renderer._sequence = SimpleNamespace(width=width, height=height)
    renderer._viewport_size = (1920, 1080)
    renderer._max_preview_edge = 3840
    renderer._preview_quality = 0.0
    renderer._decode_quality_stage = "auto2k"
    return renderer


def test_auto2k_and_full_targets_are_distinct_for_large_sequences():
    renderer = _renderer_for_geometry()

    assert renderer._auto2k_decode_target_size() == (1920, 1080)
    assert renderer._decode_target_size == (1920, 1080)

    renderer._decode_quality_stage = "full"
    assert renderer._decode_target_size == (12000, 6750)


def test_scrub_uses_auto2k_even_after_paused_full_stage():
    renderer = _renderer_for_geometry()
    renderer._decode_quality_stage = "full"

    assert renderer._scrub_render_decode_target_size() == (1920, 1080)


@pytest.mark.parametrize(
    "width,height,fps,expected_depth",
    [
        (640, 360, 24.0, 4),
        (3840, 2160, 24.0, 3),
        (3840, 2160, 60.0, 2),
    ],
)
def test_prefetch_depth_stays_between_two_and_four(
    width, height, fps, expected_depth
):
    renderer = _renderer_for_geometry(width, height)
    renderer._sequence.fps = fps
    renderer._prebuffer_lookahead_frames = 4
    renderer._prebuffer_stats = {"last_depth": 0}

    assert renderer._adaptive_playback_prefetch_depth() == expected_depth
    assert 2 <= renderer.playback_prefetch_diagnostics()["adaptive_depth"] <= 4


def test_explicit_preview_quality_remains_capped_during_full_stage():
    renderer = _renderer_for_geometry()
    renderer._preview_quality = 0.5
    renderer._decode_quality_stage = "full"

    assert renderer._decode_target_size == (3840, 2160)


def test_render_worker_queue_replaces_stale_backlog():
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._stop_flag = False
    worker._queue = queue.Queue(maxsize=2)

    assert worker.enqueue(10, None, 3)
    assert worker.enqueue(11, None, 3)
    assert worker.enqueue(12, None, 3)

    task = worker._queue.get_nowait()
    assert task[0] == 12
    assert worker._queue.empty()


def test_full_quality_refresh_is_dropped_when_playback_resumes(monkeypatch):
    renderer = _renderer_for_geometry()
    renderer._quality_transition_token = 7
    renderer._full_quality_refresh_pending = True
    renderer._scrub_active = False
    renderer._engine_playback_active = True
    renderer._is_controller_playing = lambda: True
    renderer._set_decode_quality_stage = lambda *args: pytest.fail(
        "FULL stage must not be entered after playback resumes"
    )

    renderer._submit_full_quality_refresh(7)

    assert renderer._full_quality_refresh_pending is False
