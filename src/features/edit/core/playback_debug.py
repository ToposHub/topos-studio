"""Terminal trace helpers for edit-page playback debugging."""

from __future__ import annotations

import itertools
import os
import threading
import time
from typing import Any


_TRACE_MODE = os.environ.get("TOPOS_EDIT_PLAYBACK_TRACE", "off").lower()
_ENABLED = _TRACE_MODE not in {
    "0",
    "false",
    "off",
    "no",
}
_FULL_TRACE = _TRACE_MODE in {"1", "true", "on", "yes", "full", "verbose", "all"}


def _parse_float_env(name: str, default: float) -> float:
    """安全解析外部环境变量为 float（AUD-17003）。

    环境变量是外部输入：空串/非数字（如 "4O"）不得在模块导入期抛
    ValueError 拖垮所有依赖模块，一律回退默认值。
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_SLOW_THRESHOLD_MS = _parse_float_env("TOPOS_EDIT_PLAYBACK_SLOW_MS", 40.0)
_SUMMARY_STAGES = {
    "edit_page.scrub_started",
    "edit_page.scrub_finished",
    "edit_page.timeline_scrub_frame_requested_emit",
    "edit_page.timeline_scrub_frame_requested_schedule",
    "edit_page.timeline_scrub_frame_requested_coalesced",
    "edit_page.set_timeline_preview_skipped",
    "edit_controller.preview_request_skipped",
    "timeline_panel.toggle_play",
    "controller.play",
    # P1-01: engine 路由/背压证据（唯一入口审计需要可见 miss）
    "renderer.request_engine_miss",
    "renderer.request_engine_fallback",
    "engine.back_pressure_drop",
    "controller.stop",
    "timeline_panel.controller_state_changed",
    "timer.start",
    "timer.stop",
    "renderer.gpu_result_drop_playback_stale",
    "renderer.gpu_result_drop_monotonic",
    "renderer.gpu_result_drop_generation",
    "renderer.scrub_started",
    "renderer.scrub_input",
    "renderer.scrub_preview_request",
    "renderer.scrub_exact_coalesced_schedule",
    "renderer.scrub_exact_worker_queued",
    "renderer.scrub_exact_worker_rejected",
    "renderer.scrub_settled_exact_schedule",
    "renderer.scrub_preposition",
    "renderer.scrub_gpu_submitted_direct_release",
    "renderer.scrub_gpu_submitted_release",
    "renderer.scrub_cpu_ready_latency",
    "renderer.scrub_gpu_ready_latency",
    "renderer.scrub_frame_ready_latency",
    "renderer.scrub_finished_exact_request",
    "renderer.advance_paused_no_self_pace",
    "renderer.jump_to_current_after_stale",
    "decode_manager.create_source",
    "decode_manager.interactive_progress_frame",
    "decode_manager.get_frame_done",
    "decode_manager.get_frame_failed",
    "decode_manager.get_frame_error",
    "worker.gpu_submitted_return",
    "worker.scrub_stale_drop",
    "edit_page.audio_scrub_preview_request",
    "audio_manager.scrub_preview_request",
    "audio_manager.scrub_preview_written",
    "audio_manager.scrub_preview_drop_output_full",
    "edit_page.audio_prepare_for_playback",
    "audio_manager.prepare_for_playback",
    "audio_manager.play_from_preroll",
    "audio_manager.play",
    "audio_manager.pause",
    "audio_manager.stop",
    "audio_manager.quiesce_for_timeline_rebuild",
    "audio_manager.track_sync",
    "audio_manager.track_sync_preroll",
    "audio_manager.track_sync_skipped",
    "audio_output.start",
    "audio_output.start_failed",
    "audio_output.write_device_deleted",
    "audio_scrub_worker.enqueue",
    "audio_scrub_worker.mix_done",
    "audio_scrub_worker.drop_stale_after_mix",
    # F2.5: 多机位实时网格监看流（掉帧/降质可解释）
    "multicam_grid.session_summary",
    "multicam_grid.degrade",
    "audio_thread.preroll_start",
    "audio_thread.preroll_done",
    "audio_thread.first_write",
    "audio_thread.write_zero",
    "audio_thread.error",
}
_SCRUB_TASK_STAGES = {
    "worker.enqueue",
    "worker.enqueue_replace_full",
    "worker.dequeue_execute",
    "worker.gpu_submit",
    "gpu_renderer.submit_accept",
    "gpu_renderer.submit_drop_queued_scrub",
}
_SCRUB_PRESENT_STAGES = {
    "file_controller.timeline_preview_frame_ready",
    "program_viewer.set_frame_present_dict",
    "program_viewer.set_frame_present",
    "program_viewer.set_frame_qimage",
    "program_viewer.set_frame_texture",
}
_LOCK = threading.Lock()
_SEQ = itertools.count(1)
_T0 = time.perf_counter()


def playback_trace_enabled() -> bool:
    return _ENABLED


def reset_playback_trace(reason: str = "reset", **fields: Any) -> None:
    """Reset relative trace time and sequence numbering."""
    if not _ENABLED:
        return

    global _SEQ, _T0
    with _LOCK:
        _SEQ = itertools.count(1)
        _T0 = time.perf_counter()

    playback_trace(reason, **fields)


def playback_trace(stage: str, **fields: Any) -> None:
    """Print a single structured playback trace line to the terminal."""
    if not _ENABLED:
        return
    if not _FULL_TRACE and not _should_print_summary(stage, fields):
        return

    now = time.perf_counter()
    thread = threading.current_thread()
    # AUD-17004：seq 分配与 print 在同一临界区内完成——旧实现 print 在
    # _LOCK 之外，多线程并发 trace 会产生交错撕裂的输出行，且 seq 序号
    # 与实际打印顺序可能颠倒。
    with _LOCK:
        seq = next(_SEQ)
        elapsed_ms = (now - _T0) * 1000.0

        parts = [
            f"[EDIT_PLAYBACK_TRACE seq={seq:05d} +{elapsed_ms:9.3f}ms",
            f"thread={thread.name}",
            f"stage={stage}",
        ]
        for key, value in fields.items():
            parts.append(f"{key}={_format_value(value)}")
        parts.append("]")
        print(" ".join(parts), flush=True)


def _should_print_summary(stage: str, fields: dict[str, Any]) -> bool:
    if stage in _SUMMARY_STAGES:
        if stage == "decode_manager.get_frame_done":
            return float(fields.get("elapsed_ms", 0.0) or 0.0) >= _SLOW_THRESHOLD_MS
        if stage == "worker.gpu_submitted_return":
            return float(fields.get("elapsed_ms", 0.0) or 0.0) >= _SLOW_THRESHOLD_MS
        return True
    if stage in _SCRUB_TASK_STAGES:
        return (
            fields.get("request_kind") == "scrub"
            or fields.get("scrub_token") not in {"", None}
        )
    if stage in _SCRUB_PRESENT_STAGES:
        return bool(fields.get("scrub_active", False))
    if "error" in stage or "failed" in stage or "drop" in stage:
        return True
    return False


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    text = str(value)
    if len(text) > 160:
        text = text[:157] + "..."
    if any(ch.isspace() for ch in text):
        return repr(text)
    return text
