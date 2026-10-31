#!/usr/bin/env python3
"""Run full-length, real-media TOPOS HQ exports through the product timeline pipeline.

This benchmark creates a Quick Project + a one-clip timeline through the edit
models/commands, saves a real .topos project package, then exports every source
frame with the default VideoFrameProviderService and TimelineExporter.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

os.environ["TOPOS_EXPORT_GPU_RENDER"] = "1"

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent
OUT_DIR.mkdir(parents=True, exist_ok=True)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RESULTS: dict[str, Any] = {
    "protocol": "full_length_real_project_timeline_export",
    "date": "2026-09-13",
    "host": {},
    "cases": [],
}
MAX_FRAMES = max(0, int(os.environ.get("TOPOS_EXPORT_BENCH_MAX_FRAMES", "0")))
CASE_FILTER = os.environ.get("TOPOS_EXPORT_BENCH_CASE", "all").strip().lower()


def _optional_int_env(name: str, minimum: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# 0 is meaningful for FFmpeg/PyAV decoder auto-threading. The TOPOS native
# slice pool accepts explicit sizes in the 1..16 range.
ENCODER_THREADS = _optional_int_env("TOPOS_EXPORT_ENCODER_THREADS", 1)
DECODE_THREADS = _optional_int_env("TOPOS_EXPORT_DECODE_THREADS", 0)
RUN_LABEL = os.environ.get("TOPOS_EXPORT_BENCH_LABEL")
if not RUN_LABEL:
    if CASE_FILTER == "all" and ENCODER_THREADS is None and DECODE_THREADS is None:
        RUN_LABEL = "profile_default"
    else:
        enc_label = "product" if ENCODER_THREADS is None else str(ENCODER_THREADS)
        dec_label = "default" if DECODE_THREADS is None else str(DECODE_THREADS)
        RUN_LABEL = f"profile_{CASE_FILTER}_enc{enc_label}_dec{dec_label}"
RESULTS_PATH = OUT_DIR / f"results_{RUN_LABEL}.json"


def _host_info() -> dict[str, Any]:
    brand = ""
    try:
        p = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            check=False, capture_output=True, text=True, timeout=3,
        )
        brand = p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        load = tuple(float(v) for v in os.getloadavg())
    except (AttributeError, OSError):
        load = ()
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_brand_sysctl": brand,
        "python": sys.version.split()[0],
        "cpu_count_logical": os.cpu_count(),
        "load_average_before": load,
    }


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "sum_s": 0.0, "mean_ms": None,
                "median_ms": None, "p95_ms": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "count": len(values),
        "sum_s": round(sum(values), 6),
        "mean_ms": round(statistics.mean(values) * 1000, 3),
        "median_ms": round(statistics.median(values) * 1000, 3),
        "p95_ms": round(p95 * 1000, 3),
    }


def _install_gpu_method_timers(active: list[dict[str, Any] | None]) -> None:
    from src.features.edit.core.timeline_gpu_export_renderer import (
        TimelineGpuExportRenderer,
    )

    for method_name in ("render_planar", "render_packed_bgr", "render_layer_stack"):
        original = getattr(TimelineGpuExportRenderer, method_name)

        def _make_wrapper(name, method):
            def _wrapped(self, *args, **kwargs):
                started = time.perf_counter()
                result = method(self, *args, **kwargs)
                elapsed = time.perf_counter() - started
                metrics = active[0]
                if metrics is not None:
                    metrics["gpu_method_times"].setdefault(name, []).append(elapsed)
                    metrics["gpu_renderers"][id(self)] = self
                return result
            return _wrapped

        setattr(TimelineGpuExportRenderer, method_name,
                _make_wrapper(method_name, original))


def _install_decoder_thread_override(
    active: list[dict[str, Any] | None],
) -> Callable[[], None]:
    """Set the dedicated planar source's FFmpeg thread count before first decode."""
    if DECODE_THREADS is None:
        return lambda: None

    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    original_read_frame = FFmpegMediaSource.read_frame

    def _read_frame_with_thread_override(source: Any, frame_index: int) -> Any:
        metrics = active[0]
        is_planar_export_source = (
            bool(getattr(source, "_preserve_planes", False))
            and getattr(source, "_output_mode", None) == "planar_yuv"
        )
        if (metrics is not None and is_planar_export_source
                and metrics.get("decoder_thread_override") is None):
            stream = getattr(source, "_video_stream", None)
            codec_context = getattr(stream, "codec_context", None)
            if codec_context is None:
                raise RuntimeError(
                    "Planar export FFmpeg source has no codec_context before first read_frame")
            threads_before = int(codec_context.thread_count)
            codec_context.thread_count = int(DECODE_THREADS)
            threads_after = int(codec_context.thread_count)
            metrics["decoder_thread_override"] = {
                "source_path": str(getattr(source, "path", "")),
                "first_frame_index": int(frame_index),
                "threads_before": threads_before,
                "threads_requested": int(DECODE_THREADS),
                "threads_configured": threads_after,
            }
        return original_read_frame(source, frame_index)

    FFmpegMediaSource.read_frame = _read_frame_with_thread_override

    def _restore() -> None:
        FFmpegMediaSource.read_frame = original_read_frame

    return _restore


def _save_project(
    filepath: Path,
    project: Any,
    sequence: Any,
    app: Any,
) -> bool:
    from src.features.edit.core.edit_project_io import (
        EditProjectIO,
        EditProjectIOConfig,
    )

    io = EditProjectIO(EditProjectIOConfig(
        autosave_interval=0,
        backup_on_save=False,
    ))
    saved = io.save_project(
        filepath,
        project,
        timeline_sequences={sequence.timeline_id: sequence},
        create_backup=False,
        use_ai_callback=False,
        force_project_mode="edit_project",
    )
    if not saved:
        logging.error("Project package save failed: %s", filepath)
    # Keep QObject alive until save_project has completed synchronously.
    _ = (io, app)
    return bool(saved)


def _verify_output(path: Path, expected_frames: int) -> dict[str, Any]:
    from src.shared.codec import topos_binding as tb

    codec = tb.ToposCodec()
    movie = tb.ToposMovieFile(codec, str(path))
    try:
        info = movie.info()
        sample_count = int(info.sample_count)
        decoded_edges: list[dict[str, Any]] = []
        for index in sorted({0, max(0, sample_count - 1)}):
            frame = codec.decode(movie.packet(index))
            decoded_edges.append({
                "index": index,
                "width": int(frame.info.visible_width),
                "height": int(frame.info.visible_height),
            })
        return {
            "sample_count": sample_count,
            "expected_frames": expected_frames,
            "frame_count_matches": sample_count == expected_frames,
            "decoded_first_and_last_frames": decoded_edges,
        }
    finally:
        movie.close()


def _run_case(media_path: Path, app: Any, gpu_metrics_active: list) -> dict[str, Any]:
    from src.features.edit.core.commands import AddClipCommand, CommandStack
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.video.frame_provider_service import get_frame_provider_service

    case_name = "2k" if media_path.name.startswith("2K_") else "4k"
    setup_started = time.perf_counter()
    metadata = MediaSourceFactory.probe_video_metadata(str(media_path))
    if metadata is None:
        raise RuntimeError(f"Could not probe source media: {media_path}")
    width, height = int(metadata.width), int(metadata.height)
    fps = metadata.fps
    source_frames = int(metadata.total_frames or 0)
    if source_frames <= 0:
        raise RuntimeError(f"Source frame count unavailable: {media_path}")
    total_frames = min(source_frames, MAX_FRAMES) if MAX_FRAMES else source_frames

    project, asset_id, timeline_id = EditProject.create_quick_project(
        name=f"TOPOS full export benchmark {case_name.upper()}",
        media_path=str(media_path),
        fps=float(fps),
        width=width,
        height=height,
        timeline_name=f"{case_name.upper()} no-effects timeline",
    )
    track_id = "video-1"
    empty_track = EditTrack(
        track_id=track_id,
        name="Video 1",
        track_type="video",
        index=0,
    )
    sequence = TimelineSequence(
        timeline_id=timeline_id,
        name=f"{case_name.upper()} no-effects timeline",
        fps=fps,
        width=width,
        height=height,
        video_tracks=(empty_track,),
    )
    clip = ClipInstance(
        clip_id=f"{case_name}-source-clip",
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=0,
        timeline_duration=total_frames,
        source_start=0,
        source_duration=total_frames,
        _source_fps_num=fps.num,
        _source_fps_den=fps.den,
        _timeline_fps_num=fps.num,
        _timeline_fps_den=fps.den,
    )
    sequence = CommandStack().execute(
        AddClipCommand.create(track_id, clip), sequence)

    project_path = OUT_DIR / f"{case_name}_no_effects_project_{RUN_LABEL}.topos"
    project_saved = _save_project(project_path, project, sequence, app)
    setup_wall = time.perf_counter() - setup_started

    output_path = OUT_DIR / f"{case_name}_no_effects_topos_hq_{RUN_LABEL}.mov"
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output_path}")

    metrics: dict[str, Any] = {
        "project_name": project.name,
        "project_id": project.project_id,
        "timeline_id": sequence.timeline_id,
        "asset_id": asset_id,
        "clip_count": sum(len(t.clips) for t in sequence.video_tracks),
        "effect_count": sum(
            len(clip.effects)
            for track in sequence.video_tracks for clip in track.clips
        ),
        "expected_frames": total_frames,
        "source_fps": float(fps),
        "resolution": [width, height],
        "frame_request_times": [],
        "master_decode_times": [],
        "high_depth_decode_times": [],
        "render_frame_times": [],
        "gpu_composite_times": [],
        "gpu_method_times": {},
        "encode_frame_times": [],
        "pipeline_scan_times": [],
        "encoder_open_times": [],
        "encoder_close_times": [],
        "gpu_success_frames": 0,
        "gpu_attempt_frames": 0,
        "direct_planar_success_frames": 0,
        "direct_planar_attempts": 0,
        "direct_planar_times": [],
        "decoder_thread_override": None,
        "decoder_threads_requested": DECODE_THREADS,
        "encoder_threads_requested": ENCODER_THREADS,
        "provider_frame_metadata": [],
        "master_frame_metadata": [],
        "high_depth_frame_metadata": [],
        "gpu_renderers": {},
        "progress_first_time": None,
        "progress_last_time": None,
        "progress_last_frame": 0,
        "progress_events": 0,
        "provider_sources": [],
        "provider_vt_source_present": None,
        "project_path": str(project_path),
        "project_saved": project_saved,
        "output_path": str(output_path),
        "quality_setting": "profile default rate control (quality=None, mode=auto)",
    }

    provider = get_frame_provider_service()
    original_open_clip = provider.open_clip
    original_request_frame = provider.request_frame

    def _timed_open_clip(*args, **kwargs):
        clip_id = original_open_clip(*args, **kwargs)
        session = provider._clips.get(clip_id)
        if session is not None:
            source = session.master_source
            metrics["provider_sources"].append(
                type(source).__name__ if source is not None else None)
            metrics["provider_vt_source_present"] = session.vt_source is not None
        return clip_id

    def _timed_request_frame(*args, **kwargs):
        started = time.perf_counter()
        try:
            result = original_request_frame(*args, **kwargs)
        finally:
            metrics["frame_request_times"].append(
                time.perf_counter() - started)
        if result is not None and not metrics["provider_frame_metadata"]:
            metrics["provider_frame_metadata"].append({
                "pixel_format": str(getattr(result, "pixel_format", "")),
                "has_planes": bool(getattr(result, "planes", None)),
                "data_dtype": str(getattr(getattr(result, "data", None), "dtype", "")),
                "data_shape": list(getattr(getattr(result, "data", None), "shape", ())),
            })
        return result

    provider.open_clip = _timed_open_clip
    provider.request_frame = _timed_request_frame

    exporter = TimelineExporter(
        project=project,
        sequence=sequence,
        frame_provider=provider,
        progress_callback=lambda current, total, _fraction: _on_progress(
            metrics, current, total),
    )

    def _wrap_timed_method(name: str, target_list: list[float]) -> None:
        original = getattr(exporter, name)

        def _wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = original(*args, **kwargs)
                if name == "_pipeline_eligible":
                    metrics["pipeline_eligible"] = bool(result)
                elif (name == "_request_high_depth_decoded_frame"
                      and metrics.get("high_depth_decoder_threads_product") is None):
                    for source in getattr(
                            exporter, "_export_high_depth_sources", {}).values():
                        context = getattr(
                            getattr(source, "_video_stream", None),
                            "codec_context", None)
                        if context is not None:
                            metrics["high_depth_decoder_threads_product"] = int(
                                context.thread_count)
                            break
                return result
            finally:
                target_list.append(time.perf_counter() - started)

        setattr(exporter, name, _wrapped)

    _wrap_timed_method("_request_video_decoded_frame", metrics["master_decode_times"])
    _wrap_timed_method("_request_high_depth_decoded_frame", metrics["high_depth_decode_times"])
    _wrap_timed_method("_render_frame", metrics["render_frame_times"])
    _wrap_timed_method("_gpu_render_composite", metrics["gpu_composite_times"])
    _wrap_timed_method("_pipeline_eligible", metrics["pipeline_scan_times"])
    _wrap_timed_method("_encode_frame", metrics["encode_frame_times"])

    original_passthrough = exporter._try_render_planar_passthrough

    def _count_planar_passthrough(*args, **kwargs):
        metrics["direct_planar_attempts"] += 1
        started = time.perf_counter()
        try:
            result = original_passthrough(*args, **kwargs)
            if result is not None:
                metrics["direct_planar_success_frames"] += 1
            return result
        finally:
            metrics["direct_planar_times"].append(time.perf_counter() - started)

    exporter._try_render_planar_passthrough = _count_planar_passthrough

    original_create_encoder = exporter._create_encoder

    def _create_encoder_timed(*args, **kwargs):
        encoder = original_create_encoder(*args, **kwargs)
        if ENCODER_THREADS is not None:
            # Apply after the product factory chooses the encoder, but before
            # TimelineExporter calls open() and before first packet encoding.
            encoder._slice_threads = ENCODER_THREADS
        metrics["encoder_slice_threads"] = getattr(encoder, "_slice_threads", None)
        metrics["encoder_slice_threads_configured"] = getattr(
            encoder, "_slice_threads", None)
        original_encode_packet = getattr(encoder, "_encode_packet", None)
        if callable(original_encode_packet):
            def _capture_encoder_thread_count(*packet_args, **packet_kwargs):
                result = original_encode_packet(*packet_args, **packet_kwargs)
                if metrics.get("encoder_slice_threads_actual") is None:
                    codec = getattr(encoder, "_codec", None)
                    if codec is not None:
                        metrics["encoder_slice_threads_actual"] = int(
                            codec.slice_threads())
                return result

            encoder._encode_packet = _capture_encoder_thread_count
        for method_name, target in (
            ("open", metrics["encoder_open_times"]),
            ("close", metrics["encoder_close_times"]),
        ):
            original_method = getattr(encoder, method_name)

            def _make_encoder_timer(method, timing_list):
                def _timed(*method_args, **method_kwargs):
                    started = time.perf_counter()
                    try:
                        return method(*method_args, **method_kwargs)
                    finally:
                        timing_list.append(time.perf_counter() - started)
                return _timed

            setattr(encoder, method_name,
                    _make_encoder_timer(original_method, target))
        metrics["encoder_pixel_format"] = getattr(
            getattr(encoder, "_config", None), "pix_fmt", None)
        return encoder

    exporter._create_encoder = _create_encoder_timed

    original_gpu_composite = exporter._gpu_render_composite

    def _gpu_composite_counted(*args, **kwargs):
        started = time.perf_counter()
        metrics["gpu_attempt_frames"] += 1
        result = original_gpu_composite(*args, **kwargs)
        elapsed = time.perf_counter() - started
        # _gpu_render_composite's generic timer already records this method.
        renderer = exporter._gpu_export_renderer
        if renderer is not None:
            metrics["gpu_renderers"][id(renderer)] = renderer
            ctx = getattr(renderer, "_ctx", None)
            if ctx is not None:
                info = ctx.info
                metrics["gl_context"] = {
                    "renderer": str(info.get("GL_RENDERER", "?")),
                    "vendor": str(info.get("GL_VENDOR", "?")),
                    "version": str(info.get("GL_VERSION", "?")),
                }
        if result is not None:
            metrics["gpu_success_frames"] += 1
        metrics["gpu_composite_inclusive_s"] = (
            metrics.get("gpu_composite_inclusive_s", 0.0) + elapsed)
        return result

    # Replace after the generic timer so counts wrap the timed method once.
    exporter._gpu_render_composite = _gpu_composite_counted

    original_master_request = exporter._request_video_decoded_frame

    def _capture_master_frame(*args, **kwargs):
        result = original_master_request(*args, **kwargs)
        if result is not None and not metrics["master_frame_metadata"]:
            metrics["master_frame_metadata"].append({
                "pixel_format": str(getattr(result, "pixel_format", "")),
                "has_planes": bool(getattr(result, "planes", None)),
                "data_dtype": str(getattr(getattr(result, "data", None), "dtype", "")),
                "data_shape": list(getattr(getattr(result, "data", None), "shape", ())),
            })
        return result

    # Retain the timing wrapper while adding metadata capture.
    exporter._request_video_decoded_frame = _capture_master_frame

    original_hd_request = exporter._request_high_depth_decoded_frame

    def _capture_high_depth_frame(*args, **kwargs):
        result = original_hd_request(*args, **kwargs)
        if result is not None and not metrics["high_depth_frame_metadata"]:
            metrics["high_depth_frame_metadata"].append({
                "pixel_format": str(getattr(result, "pixel_format", "")),
                "has_planes": bool(getattr(result, "planes", None)),
                "plane_shapes": [list(p.shape) for p in (getattr(result, "planes", ()) or ())],
                "data_dtype": str(getattr(getattr(result, "data", None), "dtype", "")),
            })
        return result

    exporter._request_high_depth_decoded_frame = _capture_high_depth_frame

    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id=timeline_id,
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.TOPOS_HQ,
        output_path=str(output_path),
        has_alpha=False,
        include_audio=False,
        burn_subtitles=False,
        # Match the product's profile-controlled TOPOS default. CRF 6 is a
        # deliberate near-lossless override and was used only in the first
        # diagnostic pass, not as the default tier rate-control setting.
        quality=None,
        quality_mode="auto",
        resolution=(width, height),
        fps=float(fps),
    )

    print(
        f"\n[{case_name.upper()}] {media_path.name}: "
        f"{width}x{height} @ {float(fps):g} fps, {total_frames} frames "
        f"({total_frames / float(fps):.2f}s), source={media_path.stat().st_size / 1e9:.2f} GB",
        flush=True,
    )
    gpu_metrics_active[0] = metrics
    started = time.perf_counter()
    outcome = exporter.export_timeline(config)
    export_wall = time.perf_counter() - started
    gpu_metrics_active[0] = None

    if not outcome.success:
        raise RuntimeError(
            f"Timeline export failed for {media_path.name}: {outcome!r}"
        )
    if not output_path.is_file():
        raise RuntimeError(f"Exporter reported success but output is missing: {output_path}")

    output_bytes = output_path.stat().st_size
    output_check = _verify_output(output_path, total_frames)
    duration = output_check["sample_count"] / float(fps)
    metrics.update({
        "source_path": str(media_path),
        "source_size_bytes": media_path.stat().st_size,
        "source_duration_s": round(total_frames / float(fps), 6),
        "source_pixel_format": metadata.pixel_format,
        "source_codec": metadata.codec_name,
        "output_size_bytes": output_bytes,
        "output_duration_s": round(duration, 6),
        "output_bitrate_mbps": round(output_bytes * 8 / duration / 1_000_000, 3),
        "output_check": output_check,
        "project_setup_and_save_s": round(setup_wall, 6),
        "export_wall_s": round(export_wall, 6),
        "export_fps_including_setup_close": round(total_frames / export_wall, 3),
        "realtime_multiple": round((total_frames / export_wall) / float(fps), 3),
        "frames_encoded_reported": int(getattr(exporter, "_current_frame", 0)),
        "pipeline_eligible": metrics.get("pipeline_eligible"),
        "provider_total_frames_served": int(getattr(provider, "total_frames_served", 0)),
        "provider_cache_hit_rate": round(float(getattr(provider, "cache_hit_rate", 0.0)), 4),
    })
    if metrics["progress_first_time"] is not None and metrics["progress_last_time"] is not None:
        metrics["frame_progress_wall_s"] = round(
            metrics["progress_last_time"] - metrics["progress_first_time"], 6)
        metrics["frame_progress_fps"] = round(
            metrics["progress_last_frame"] /
            max(metrics["frame_progress_wall_s"], 1e-9), 3)

    for metric_key, values in (
        ("frame_request", metrics["frame_request_times"]),
        ("master_decode_call", metrics["master_decode_times"]),
        ("high_depth_planar_decode_call", metrics["high_depth_decode_times"]),
        ("render_frame", metrics["render_frame_times"]),
        ("gpu_composite_inclusive", metrics["gpu_composite_times"]),
        ("encode_frame", metrics["encode_frame_times"]),
        ("planar_passthrough", metrics["direct_planar_times"]),
        ("pipeline_pre_scan", metrics["pipeline_scan_times"]),
        ("encoder_open", metrics["encoder_open_times"]),
        ("encoder_close_finalize", metrics["encoder_close_times"]),
    ):
        metrics[f"stage_{metric_key}"] = _summary(values)
    metrics["stage_gpu_upload_render_readback"] = {
        name: _summary(times)
        for name, times in metrics["gpu_method_times"].items()
    }
    renderers = list(metrics["gpu_renderers"].values())
    metrics["gpu_renderer_status"] = [{
        "frames_rendered": int(getattr(renderer, "frames_rendered", 0)),
        "gl_errors": int(getattr(renderer, "gl_errors", 0)),
        "failed": bool(getattr(renderer, "failed", False)),
        "gl_context_created": getattr(renderer, "_ctx", None) is not None,
    } for renderer in renderers]
    # Keep live renderer objects only until their scalar counters are captured.
    metrics.pop("gpu_renderers", None)
    metrics["gpu_frames_fallback_or_ineligible"] = (
        total_frames - int(metrics["gpu_success_frames"])
    )
    metrics["setup_notes"] = [
        "Frames were read live from the original media through VideoFrameProviderService; no predecoded frame cache or looped frame fixture was used.",
        "The output uses the TOPOS HQ profile and source resolution/frame rate.",
        "Stage timing sums overlap because the product overlaps render and encode with a bounded queue.",
        "export_fps_including_setup_close divides all timeline frames by export_timeline wall time, including preflight, pipeline scan, encoder open/finalize, and mux close.",
    ]
    print(
        f"[{case_name.upper()}] done: {export_wall:.2f}s, "
        f"{total_frames / export_wall:.2f} fps, "
        f"encode_threads={metrics.get('encoder_slice_threads_actual', 'unobserved')}, "
        f"decode_threads={((metrics.get('decoder_thread_override') or {}).get('threads_configured', 'default'))}, "
        f"{metrics['output_bitrate_mbps']:.1f} Mb/s, "
        f"planar={metrics['direct_planar_success_frames']}/{total_frames}, "
        f"GPU={metrics['gpu_success_frames']}/{total_frames} frames "
        f"{metrics.get('gl_context', {}).get('renderer', 'unavailable/fallback')}",
        flush=True,
    )
    return metrics


def _on_progress(metrics: dict[str, Any], current: int, total: int) -> None:
    now = time.perf_counter()
    if metrics["progress_first_time"] is None:
        metrics["progress_first_time"] = now
    metrics["progress_last_time"] = now
    metrics["progress_last_frame"] = int(current)
    metrics["progress_events"] += 1
    stride = max(1, int(total) // 20)
    if current == 1 or current == total or current % stride == 0:
        print(f"  progress {current}/{total} ({100 * current / total:.1f}%)", flush=True)


def main() -> None:
    if CASE_FILTER not in ("all", "2k", "4k"):
        raise ValueError(
            "TOPOS_EXPORT_BENCH_CASE must be all, 2k, or 4k; "
            f"got {CASE_FILTER!r}")
    log_path = OUT_DIR / f"full_export_{RUN_LABEL}.log"
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[file_handler, stream_handler],
        force=True,
    )
    RESULTS["host"] = _host_info()
    RESULTS["benchmark_options"] = {
        "case_filter": CASE_FILTER,
        "encoder_threads_requested": ENCODER_THREADS,
        "decode_threads_requested": DECODE_THREADS,
        "max_frames": MAX_FRAMES,
    }
    print(json.dumps(RESULTS["host"], ensure_ascii=False), flush=True)

    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    gpu_metrics_active: list[dict[str, Any] | None] = [None]
    _install_gpu_method_timers(gpu_metrics_active)
    restore_decoder_thread_override = _install_decoder_thread_override(
        gpu_metrics_active)

    all_media_paths = [
        ROOT / "tests/test_videos/2K_prores422HQ.mov",
        ROOT / "tests/test_videos/4K_prores422HQ.mov",
    ]
    media_paths = [
        path for path in all_media_paths
        if CASE_FILTER == "all" or path.name.lower().startswith(CASE_FILTER)
    ]
    for media_path in media_paths:
        if not media_path.is_file():
            raise FileNotFoundError(media_path)
        case_metrics = _run_case(media_path, app, gpu_metrics_active)
        RESULTS["cases"].append(case_metrics)
        RESULTS["host"]["load_average_after_case"] = (
            tuple(float(v) for v in os.getloadavg())
            if hasattr(os, "getloadavg") else ()
        )
        RESULTS_PATH.write_text(
            json.dumps(RESULTS, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    restore_decoder_thread_override()

    from src.shared.video.frame_provider_service import get_frame_provider_service

    get_frame_provider_service().shutdown()
    RESULTS["host"]["load_average_after"] = (
        tuple(float(v) for v in os.getloadavg())
        if hasattr(os, "getloadavg") else ()
    )
    RESULTS_PATH.write_text(
        json.dumps(RESULTS, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nWrote {RESULTS_PATH}", flush=True)


if __name__ == "__main__":
    main()
