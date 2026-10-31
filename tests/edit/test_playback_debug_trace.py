from __future__ import annotations

from src.features.edit.core import playback_debug


def test_scrub_worker_stages_are_visible_in_summary_trace() -> None:
    assert playback_debug._should_print_summary(
        "worker.dequeue_execute",
        {"request_kind": "scrub", "queue_wait_ms": 1.0},
    )
    assert playback_debug._should_print_summary(
        "worker.gpu_submit",
        {"scrub_token": 12},
    )
    assert not playback_debug._should_print_summary(
        "worker.dequeue_execute",
        {"request_kind": "playback", "queue_wait_ms": 1.0},
    )


def test_scrub_present_stages_require_active_scrub_context() -> None:
    assert playback_debug._should_print_summary(
        "file_controller.timeline_preview_frame_ready",
        {"scrub_active": True},
    )
    assert playback_debug._should_print_summary(
        "program_viewer.set_frame_texture",
        {"scrub_active": True},
    )
    assert not playback_debug._should_print_summary(
        "program_viewer.set_frame_texture",
        {"scrub_active": False},
    )


def test_scrub_latency_stages_are_visible_in_summary_trace() -> None:
    assert playback_debug._should_print_summary(
        "renderer.scrub_gpu_ready_latency",
        {"elapsed_ms": 8.0},
    )
    assert playback_debug._should_print_summary(
        "renderer.scrub_frame_ready_latency",
        {"elapsed_ms": 9.0},
    )


def test_interactive_progress_frame_is_visible_in_summary_trace() -> None:
    assert playback_debug._should_print_summary(
        "decode_manager.interactive_progress_frame",
        {"frame": 35, "decoded_frame": 24},
    )


def test_scrub_gpu_submit_release_is_visible_in_summary_trace() -> None:
    assert playback_debug._should_print_summary(
        "renderer.scrub_gpu_submitted_direct_release",
        {"frame": 143, "latest": 142},
    )
    assert playback_debug._should_print_summary(
        "renderer.scrub_gpu_submitted_release",
        {"frame": 143, "latest": 142},
    )


def test_audio_preroll_stages_are_visible_in_summary_trace() -> None:
    assert playback_debug._should_print_summary(
        "edit_page.audio_scrub_preview_request",
        {"frame": 100},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.scrub_preview_request",
        {"frame": 100},
    )
    assert playback_debug._should_print_summary(
        "audio_scrub_worker.mix_done",
        {"frame": 100},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.scrub_preview_written",
        {"frame": 100, "written": 512},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.prepare_for_playback",
        {"frame": 100},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.quiesce_for_timeline_rebuild",
        {},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.track_sync_skipped",
        {"tracks": 1},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.track_sync_preroll",
        {"frame": 100},
    )
    assert playback_debug._should_print_summary(
        "audio_thread.preroll_done",
        {"frame": 100, "filled": 4800},
    )
    assert playback_debug._should_print_summary(
        "audio_manager.play_from_preroll",
        {"start_frame": 100},
    )
    assert playback_debug._should_print_summary(
        "audio_output.write_device_deleted",
        {"error": "wrapped C/C++ object of type QIODevice has been deleted"},
    )
