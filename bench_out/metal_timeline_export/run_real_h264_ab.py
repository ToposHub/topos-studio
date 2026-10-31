#!/usr/bin/env python3
"""Real-material timeline export benchmark (Metal/OpenGL/Topos).

Example:
  TOPOS_EXPORT_BENCH_FRAMES=120 .venv/bin/python \
    bench_out/metal_timeline_export/run_real_h264_ab.py --backend metal
  TOPOS_EXPORT_BENCH_FRAMES=120 .venv/bin/python \
    bench_out/metal_timeline_export/run_real_h264_ab.py --backend metal --effect blur
  TOPOS_EXPORT_BENCH_FRAMES=120 .venv/bin/python \
    bench_out/metal_timeline_export/run_real_h264_ab.py --backend opengl
  TOPOS_EXPORT_BENCH_FRAMES=120 .venv/bin/python \
    bench_out/metal_timeline_export/run_real_h264_ab.py --backend metal \
    --codec topos_proxy

The default source is the real 1920x1080 H.264 clip in tests/test_videos.
Set TOPOS_EXPORT_BENCH_MEDIA to another real clip (for example 2K_H265.mov)
to exercise P010. Stack mode places two copies on separate tracks.
Pass --cpu-compositor with --backend opengl to time the existing CPU render
path against a GPU-rendered export with otherwise matching media settings.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MEDIA_SETTING = Path(os.environ.get(
    "TOPOS_EXPORT_BENCH_MEDIA", "tests/test_videos/2K_h264.mp4"))
MEDIA = MEDIA_SETTING if MEDIA_SETTING.is_absolute() else ROOT / MEDIA_SETTING
SECOND_MEDIA_SETTING = os.environ.get("TOPOS_EXPORT_BENCH_SECOND_MEDIA", "")
SECOND_MEDIA = Path(SECOND_MEDIA_SETTING) if SECOND_MEDIA_SETTING else None
if SECOND_MEDIA is not None and not SECOND_MEDIA.is_absolute():
    SECOND_MEDIA = ROOT / SECOND_MEDIA
FRAMES = max(1, int(os.environ.get("TOPOS_EXPORT_BENCH_FRAMES", "120")))
WIDTH = max(2, int(os.environ.get("TOPOS_EXPORT_BENCH_WIDTH", "1280")))
HEIGHT = max(2, int(os.environ.get("TOPOS_EXPORT_BENCH_HEIGHT", "720")))
OUTPUT_DIR = Path(os.environ.get(
    "TOPOS_EXPORT_BENCH_OUTPUT_DIR", "/tmp/topos_metal_timeline_bench"))
EFFECT = "none"
EFFECT_RADIUS: float | None = None
EFFECT_BLUR_TYPE = "gaussian"
EFFECT_EDGE_PRESERVE = 0.5
EFFECT_EDGE_MODE = "extend"
EFFECT_EDGE_KERNEL_SIZE = 3
EFFECT_DEFOCUS_MODE = "separable"
BLEND_MODE = "normal"
GPU_RENDER_ENABLED = True
EXPORT_CODEC = "h264"


def _probe(path: Path) -> dict[str, Any]:
    if EXPORT_CODEC == "topos_proxy":
        # FFmpeg has no Topos decoder registration. Probe the native TPIC
        # container through the same application metadata path used by the
        # editor instead of treating a valid Topos MOV as an invalid file.
        from src.shared.media.factory import MediaSourceFactory

        metadata = MediaSourceFactory.probe_video_metadata(str(path))
        if metadata is None:
            raise RuntimeError(f"Cannot probe Topos output: {path}")
        fps = getattr(metadata, "fps", None)
        return {
            "codec_name": str(getattr(metadata, "codec_name", "")),
            "width": int(getattr(metadata, "width", 0)),
            "height": int(getattr(metadata, "height", 0)),
            "avg_frame_rate": (
                f"{int(getattr(fps, 'num', 0))}/{int(getattr(fps, 'den', 1))}"
                if fps is not None else None
            ),
            "pix_fmt": str(getattr(metadata, "pixel_format", "")),
            "nb_read_frames": int(getattr(metadata, "total_frames", 0)),
        }
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,avg_frame_rate,pix_fmt,nb_read_frames",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)["streams"][0]


def _run(backend: str, mode: str) -> dict[str, Any]:
    os.environ["TOPOS_EXPORT_GPU_RENDER"] = "1" if GPU_RENDER_ENABLED else "0"
    os.environ["TOPOS_BACKEND_GRAPHICS"] = backend
    from PyQt6.QtCore import QCoreApplication
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportRangeType, ExportSource, TimelineExportConfig,
        TimelineExporter,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence,
    )
    from src.features.edit.core.clip_params import ClipBlendMode
    from src.features.edit.core.effect_instance import EffectInstance
    from src.shared.media.factory import MediaSourceFactory
    from src.shared.video.frame_provider_service import get_frame_provider_service

    app = QCoreApplication.instance() or QCoreApplication([])
    metadata = MediaSourceFactory.probe_video_metadata(str(MEDIA))
    if metadata is None:
        raise RuntimeError(f"Cannot probe source: {MEDIA}")
    fps = metadata.fps
    fps_value = float(fps)
    source_num = int(getattr(fps, "num", 25))
    source_den = int(getattr(fps, "den", 1))
    project, asset_id, timeline_id = EditProject.create_quick_project(
        name=f"Real media {backend} timeline export benchmark",
        media_path=str(MEDIA), fps=fps_value,
        width=int(metadata.width), height=int(metadata.height),
        timeline_name="Two-track normal blend benchmark",
    )
    top_asset_id = asset_id
    top_fps = fps
    top_fps_num, top_fps_den = source_num, source_den
    top_media_path = MEDIA
    if mode == "stack" and SECOND_MEDIA is not None:
        top_metadata = MediaSourceFactory.probe_video_metadata(str(SECOND_MEDIA))
        if top_metadata is None:
            raise RuntimeError(f"Cannot probe second source: {SECOND_MEDIA}")
        top_fps = top_metadata.fps
        top_fps_num = int(getattr(top_fps, "num", source_num))
        top_fps_den = int(getattr(top_fps, "den", source_den))
        existing_top_asset = project.get_asset_by_path(str(SECOND_MEDIA))
        if existing_top_asset is None:
            project = project.import_asset(
                path=str(SECOND_MEDIA), media_type="video",
                metadata={
                    "width": top_metadata.width,
                    "height": top_metadata.height,
                    "fps": float(top_fps),
                    "duration_seconds": top_metadata.duration_seconds,
                    "total_frames": top_metadata.total_frames,
                    "codec_name": top_metadata.codec_name,
                    "pixel_format": top_metadata.pixel_format,
                },
            )
            existing_top_asset = project.get_asset_by_path(str(SECOND_MEDIA))
        if existing_top_asset is None:
            raise RuntimeError(f"Failed to import second source: {SECOND_MEDIA}")
        top_asset_id = existing_top_asset.asset_id
        top_media_path = SECOND_MEDIA
    if EFFECT == "blur":
        if EFFECT_BLUR_TYPE == "bilateral":
            blur_params = {
                "blur_type": "bilateral", "radius": EFFECT_RADIUS,
                "edge_preserve": EFFECT_EDGE_PRESERVE, "mix": 1.0,
            }
        elif EFFECT_BLUR_TYPE == "motion":
            blur_params = {
                "blur_type": "motion", "radius": EFFECT_RADIUS,
                "angle": 37.0, "shutter": 1.0,
                "motion_source": "angle", "quality": "normal", "mix": 1.0,
            }
        elif EFFECT_BLUR_TYPE == "bokeh":
            blur_params = {
                "blur_type": "bokeh", "radius": EFFECT_RADIUS,
                "aperture_blades": "6", "anamorphic": 0.25,
                "highlight_gain": 0.5, "quality": "normal", "mix": 1.0,
            }
        else:
            blur_params = {
                "blur_type": EFFECT_BLUR_TYPE, "radius_x": EFFECT_RADIUS,
                "radius_y": EFFECT_RADIUS, "mix": 1.0,
            }
        effects = (EffectInstance(
            effect_type="blur",
            params=blur_params,
        ),)
    elif EFFECT == "sharpen":
        effects = (EffectInstance(
            effect_type="sharpen",
            params={"amount": 1.0, "radius": EFFECT_RADIUS or 1.0, "threshold": 0.0,
                    "channels": "all"},
        ),)
    elif EFFECT == "glow":
        effects = (EffectInstance(
            effect_type="glow",
            params={"threshold": 0.7, "radius": EFFECT_RADIUS,
                    "intensity": 0.5, "mix": 1.0},
        ),)
    elif EFFECT == "erode_dilate":
        effects = (EffectInstance(
            effect_type="erode_dilate",
            params={"operation": "dilate", "size": 5,
                    "iterations": 1, "channels": "all",
                    "edge_mode": EFFECT_EDGE_MODE},
        ),)
    elif EFFECT == "edge_detect":
        effects = (EffectInstance(
            effect_type="edge_detect",
            params={"method": "sobel", "threshold": 0.1,
                    "direction": "both", "output_mode": "edges",
                    "output_strength": 1.0,
                    "kernel_size": EFFECT_EDGE_KERNEL_SIZE,
                    "preblur": 0.0, "channel": "luminance"},
        ),)
    elif EFFECT == "denoise":
        effects = (EffectInstance(
            effect_type="denoise",
            params={"strength": 0.5, "radius": 3,
                    "luminance_only": False, "chroma_only": False},
        ),)
    elif EFFECT == "defocus":
        effects = (EffectInstance(
            effect_type="defocus",
            params={"mode": EFFECT_DEFOCUS_MODE, "focal_x": 0.5,
                    "focal_y": 0.5, "focal_size": 0.3,
                    "max_radius": 20.0, "edge_policy": "clamp",
                    "process_alpha": True},
        ),)
    elif EFFECT == "chain":
        effects = (
            EffectInstance(
                effect_type="blur",
                params={"blur_type": "gaussian", "radius_x": 3.0,
                        "radius_y": 3.0, "mix": 0.8},
                order=0,
            ),
            EffectInstance(
                effect_type="sharpen",
                params={"amount": 0.8, "radius": 1.0,
                        "threshold": 0.01, "channels": "rgb"},
                order=1,
            ),
            EffectInstance(
                effect_type="glow",
                params={"threshold": 0.7, "radius": 3.0,
                        "intensity": 0.4, "mix": 0.7,
                        "quality": "fast"},
                order=2,
            ),
        )
    else:
        effects = ()
    v1, v2 = "metal-bench-v1", "metal-bench-v2"
    base = dict(
        asset_id=asset_id, timeline_start=0, timeline_duration=FRAMES,
        source_start=0, source_duration=FRAMES,
        effects=effects,
        _source_fps_num=source_num, _source_fps_den=source_den,
        _timeline_fps_num=source_num, _timeline_fps_den=source_den,
    )
    if mode == "stack":
        clip_bottom = ClipInstance(
            clip_id="metal-bench-bottom", track_id=v1, **base)
        clip_top = ClipInstance(
            clip_id="metal-bench-top", asset_id=top_asset_id, track_id=v2,
            opacity=0.55, blend_mode=ClipBlendMode(BLEND_MODE),
            _source_fps_num=top_fps_num, _source_fps_den=top_fps_den,
            _timeline_fps_num=source_num, _timeline_fps_den=source_den,
            timeline_start=0, timeline_duration=FRAMES,
            source_start=0, source_duration=FRAMES, effects=effects)
        tracks = (
            EditTrack(track_id=v1, name="V1", track_type="video", index=0,
                      clips=(clip_bottom,)),
            EditTrack(track_id=v2, name="V2", track_type="video", index=1,
                      clips=(clip_top,)),
        )
    else:
        split = max(1, FRAMES // 2)
        clips = [ClipInstance(
            clip_id="metal-bench-segment-1", asset_id=asset_id, track_id=v1,
            timeline_start=0, timeline_duration=split,
            source_start=0, source_duration=split,
            effects=effects,
            _source_fps_num=source_num, _source_fps_den=source_den,
            _timeline_fps_num=source_num, _timeline_fps_den=source_den,
        )]
        if FRAMES > split:
            clips.append(ClipInstance(
                clip_id="metal-bench-segment-2", asset_id=asset_id, track_id=v1,
                timeline_start=split, timeline_duration=FRAMES - split,
                source_start=split, source_duration=FRAMES - split,
                effects=effects,
                _source_fps_num=source_num, _source_fps_den=source_den,
                _timeline_fps_num=source_num, _timeline_fps_den=source_den,
            ))
        tracks = (EditTrack(
            track_id=v1, name="V1", track_type="video", index=0,
            clips=tuple(clips),
        ),)
    sequence = TimelineSequence(
        timeline_id=timeline_id, name="Two-track normal blend benchmark",
        fps=fps, width=int(metadata.width), height=int(metadata.height),
        video_tracks=tracks,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    effect_tag = EFFECT
    if EFFECT_RADIUS is not None:
        effect_tag += f"_r{EFFECT_RADIUS:g}"
    if EFFECT == "blur":
        effect_tag += f"_{EFFECT_BLUR_TYPE}"
    if EFFECT == "erode_dilate":
        effect_tag += f"_{EFFECT_EDGE_MODE}"
    if EFFECT == "defocus":
        effect_tag += f"_{EFFECT_DEFOCUS_MODE}"
    if EFFECT == "edge_detect":
        effect_tag += f"_k{EFFECT_EDGE_KERNEL_SIZE}"
    if mode == "stack":
        effect_tag += f"_{BLEND_MODE}"
    output_suffix = ".mov" if EXPORT_CODEC == "topos_proxy" else ".mp4"
    output = OUTPUT_DIR / f"{backend}_{mode}_{effect_tag}_{FRAMES}f_{WIDTH}x{HEIGHT}{output_suffix}"
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {output}")

    provider = get_frame_provider_service()
    metrics: dict[str, Any] = {
        "backend_requested": backend, "mode": mode, "codec": EXPORT_CODEC,
        "blend_mode": BLEND_MODE if mode == "stack" else None,
        "gpu_render_enabled": GPU_RENDER_ENABLED,
        "render_path": "gpu" if GPU_RENDER_ENABLED else "cpu",
        "effect": EFFECT,
        "effect_radius": EFFECT_RADIUS,
        "effect_blur_type": EFFECT_BLUR_TYPE if EFFECT == "blur" else None,
        "effect_edge_preserve": (
            EFFECT_EDGE_PRESERVE if EFFECT == "blur"
            and EFFECT_BLUR_TYPE == "bilateral" else None
        ),
        "effect_edge_mode": EFFECT_EDGE_MODE if EFFECT == "erode_dilate" else None,
        "effect_edge_kernel_size": (
            EFFECT_EDGE_KERNEL_SIZE if EFFECT == "edge_detect" else None),
        "effect_mode": EFFECT_DEFOCUS_MODE if EFFECT == "defocus" else None,
        "frames_requested": FRAMES,
        "source": str(MEDIA), "source_codec": metadata.codec_name,
        "second_source": str(top_media_path) if mode == "stack" else None,
        "second_source_codec": (
            getattr(top_metadata, "codec_name", None)
            if mode == "stack" and SECOND_MEDIA is not None else None
        ),
        "source_pixel_format": metadata.pixel_format,
        "source_resolution": [int(metadata.width), int(metadata.height)],
        "output_resolution": [WIDTH, HEIGHT], "fps": fps_value,
        "decoded_frame_calls": 0, "decode_wall_s": 0.0,
        "metal_texture_frames": 0,
        "topos_metal_surface_frames": 0,
        "metal_planar_frames": 0,
        "gpu_yuv422_frames": 0,
        "gpu_composite_attempts": 0, "gpu_composite_successes": 0,
        "gpu_render_calls": 0, "gpu_render_wall_s": 0.0,
        "gpu_renderers": [],
        "render_frame_wall_s": 0.0, "encode_frame_wall_s": 0.0,
        "encoder_open_wall_s": 0.0, "encoder_close_wall_s": 0.0,
        "pre_scan_wall_s": 0.0,
        "render_encode_pipeline_eligible": None,
    }
    exporter = TimelineExporter(project, sequence, provider)
    original_decode = exporter._request_video_decoded_frame

    def timed_decode(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_decode(*args, **kwargs)
        finally:
            metrics["decoded_frame_calls"] += 1
            metrics["decode_wall_s"] += time.perf_counter() - started

    exporter._request_video_decoded_frame = timed_decode
    original_metal_decode = exporter._request_video_metal_frame

    def timed_metal_decode(*args, **kwargs):
        started = time.perf_counter()
        try:
            frame = original_metal_decode(*args, **kwargs)
            from src.shared.media.gpu_frame import GpuDecodedFrame
            if isinstance(frame, GpuDecodedFrame):
                metrics["metal_texture_frames"] += 1
                if getattr(frame, "decode_path", "") in (
                        "macos_topos_iosurface_zero_copy",
                        "macos_topos_iosurface_metal"):
                    metrics["topos_metal_surface_frames"] += 1
            return frame
        finally:
            metrics["decoded_frame_calls"] += 1
            metrics["decode_wall_s"] += time.perf_counter() - started

    if backend == "metal":
        exporter._request_video_metal_frame = timed_metal_decode
        original_planar_source = exporter._gpu_planar_source_from_decoded

        def timed_planar_source(*args, **kwargs):
            source = original_planar_source(*args, **kwargs)
            if source is not None:
                metrics["metal_planar_frames"] += 1
            return source

        exporter._gpu_planar_source_from_decoded = timed_planar_source
    original_composite = exporter._gpu_render_composite

    def timed_composite(*args, **kwargs):
        metrics["gpu_composite_attempts"] += 1
        result = original_composite(*args, **kwargs)
        if result is not None:
            metrics["gpu_composite_successes"] += 1
            if str(getattr(result, "pixel_format", "")).lower() == "yuv422p10le":
                metrics["gpu_yuv422_frames"] += 1
        renderer = exporter._gpu_export_renderer
        if renderer is not None and not any(
                item["renderer_id"] == id(renderer)
                for item in metrics["gpu_renderers"]):
            metrics["gpu_renderers"].append({
                "renderer_id": id(renderer), "class": type(renderer).__name__,
            })
        return result

    exporter._gpu_render_composite = timed_composite

    def _time_exporter_method(name: str, metric_name: str) -> None:
        original = getattr(exporter, name)

        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                metrics[metric_name] += time.perf_counter() - started

        setattr(exporter, name, wrapped)

    _time_exporter_method("_render_frame", "render_frame_wall_s")
    _time_exporter_method("_encode_frame", "encode_frame_wall_s")
    original_pipeline_eligible = exporter._pipeline_eligible

    def timed_pipeline_eligible(*args, **kwargs):
        started = time.perf_counter()
        try:
            eligible = original_pipeline_eligible(*args, **kwargs)
            metrics["render_encode_pipeline_eligible"] = bool(eligible)
            return eligible
        finally:
            metrics["pre_scan_wall_s"] += time.perf_counter() - started

    exporter._pipeline_eligible = timed_pipeline_eligible
    renderer_module_name = (
        "src.features.edit.core.timeline_metal_export_renderer"
        if backend == "metal"
        else "src.features.edit.core.timeline_gpu_export_renderer"
    )
    renderer_module = __import__(renderer_module_name, fromlist=["*"])
    renderer_class = renderer_module.TimelineMetalExportRenderer if backend == "metal" else renderer_module.TimelineGpuExportRenderer
    for method_name in (
        "render_layer_stack", "render_planar", "render_packed_bgr",
        "render_metal_texture_frame",
    ):
        if not hasattr(renderer_class, method_name):
            continue
        original_method = getattr(renderer_class, method_name)

        def make_timed(method):
            def wrapper(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    return method(self, *args, **kwargs)
                finally:
                    metrics["gpu_render_calls"] += 1
                    metrics["gpu_render_wall_s"] += time.perf_counter() - started
                    renderer = next((item for item in metrics["gpu_renderers"]
                                     if item["renderer_id"] == id(self)), None)
                    if renderer is None:
                        renderer = {"renderer_id": id(self),
                                    "class": type(self).__name__}
                        metrics["gpu_renderers"].append(renderer)
                    renderer["frames_rendered"] = int(
                        getattr(self, "frames_rendered", 0))
                    renderer["errors"] = int(getattr(
                        self, "metal_errors", getattr(self, "gl_errors", 0)))
                    renderer["last_fallback_reason"] = getattr(
                        self, "last_fallback_reason", None)
            return wrapper

        setattr(renderer_class, method_name, make_timed(original_method))

    export_codec = (ExportCodec.TOPOS_PROXY
                    if EXPORT_CODEC == "topos_proxy" else ExportCodec.H264)
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id=timeline_id,
        range_type=ExportRangeType.ENTIRE, codec=export_codec,
        output_path=str(output), has_alpha=False, include_audio=False,
        burn_subtitles=False,
        quality=(None if EXPORT_CODEC == "topos_proxy" else 23),
        quality_mode=("auto" if EXPORT_CODEC == "topos_proxy" else "crf"),
        resolution=(WIDTH, HEIGHT), fps=fps_value,
    )
    original_create_encoder = exporter._create_encoder

    def capture_encoder(*args, **kwargs):
        started = time.perf_counter()
        encoder = original_create_encoder(*args, **kwargs)
        metrics["encoder_create_wall_s"] = time.perf_counter() - started
        encoder_config = getattr(encoder, "_config", None)
        get_encoder_name = getattr(encoder_config, "get_encoder_name", None)
        hw_engaged = getattr(encoder_config, "hwaccel_engaged", None)
        metrics["encoder_name"] = (
            get_encoder_name() if callable(get_encoder_name) else None)
        metrics["hardware_encoder_engaged"] = (
            bool(hw_engaged()) if callable(hw_engaged) else None)
        for name, metric_name in (
            ("open", "encoder_open_wall_s"),
            ("close", "encoder_close_wall_s"),
        ):
            original_method = getattr(encoder, name)

            def make_timed(method, target):
                def wrapped(*method_args, **method_kwargs):
                    method_started = time.perf_counter()
                    try:
                        return method(*method_args, **method_kwargs)
                    finally:
                        metrics[target] += time.perf_counter() - method_started
                return wrapped

            setattr(encoder, name, make_timed(original_method, metric_name))
        return encoder

    exporter._create_encoder = capture_encoder
    started = time.perf_counter()
    outcome = exporter.export_timeline(config)
    export_wall = time.perf_counter() - started
    if not outcome.success:
        raise RuntimeError(f"Timeline export failed: {outcome!r}")
    metrics.update({
        "export_wall_s": round(export_wall, 6),
        "export_fps": round(FRAMES / export_wall, 3),
        "gpu_backend_resolved": (
            getattr(exporter, "_timeline_graphics_backend", None)
            if GPU_RENDER_ENABLED else None
        ),
        "output_path": str(output), "output_bytes": output.stat().st_size,
        "output_probe": _probe(output),
        "gpu_render_wall_s": round(metrics["gpu_render_wall_s"], 6),
        "decode_wall_s": round(metrics["decode_wall_s"], 6),
        "render_frame_wall_s": round(metrics["render_frame_wall_s"], 6),
        "encode_frame_wall_s": round(metrics["encode_frame_wall_s"], 6),
        "encoder_open_wall_s": round(metrics["encoder_open_wall_s"], 6),
        "encoder_close_wall_s": round(metrics["encoder_close_wall_s"], 6),
        "pre_scan_wall_s": round(metrics["pre_scan_wall_s"], 6),
    })
    metrics["frame_count_matches"] = (
        int(metrics["output_probe"].get("nb_read_frames", -1)) == FRAMES)
    metrics["frames_encoded_reported"] = int(
        getattr(exporter, "_current_frame", 0))
    metrics["notes"] = [
        "A real source is decoded by VideoFrameProviderService.",
        (f"Two video tracks are composited at 100% and 55% opacity with {BLEND_MODE} blend mode; the top source is {top_media_path}."
         if mode == "stack" else
         "Two timeline segments from the same source are placed sequentially on one video track."),
        "The complete timeline export includes decode, render, encode, mux, and close.",
        "Success manifest metadata is written before completion; multi-GB source SHA-256 is finished asynchronously.",
        ("GPU composition is disabled; the established CPU compositor renders frames."
         if not GPU_RENDER_ENABLED else
         ("Metal uses VideoToolbox NV12/P010 CVMetalTexture input when available; "
          "Topos 422 master uses IOSurface-backed Y/U/V textures; unsupported "
          "formats fall back to CPU frames."
          if backend == "metal" else
          "OpenGL uses CPU-decoded frames uploaded into its GPU renderer.")),
        ("The CPU compositor output is passed to the FFmpeg/VideoToolbox encoder."
         if not GPU_RENDER_ENABLED else
         ("Metal emits native YUV422P10 planes on the GPU for the Topos 422 Proxy "
          "encoder; the CPU only forwards those planes to the native encoder. "
          "The final plane readback is still a synchronization boundary, not an "
          "encoder-native zero-copy surface path."
          if EXPORT_CODEC == "topos_proxy" else
          "The final GPU render is read back for FFmpeg/VideoToolbox encoding; "
          "this is not an encoder-native zero-copy surface path.")),
    ]
    metrics["metal_fallback_reasons"] = sorted(
        getattr(exporter, "_metal_fallback_warnings", ()))
    provider.shutdown()
    _ = app
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("metal", "opengl"), required=True)
    parser.add_argument(
        "--codec", choices=("h264", "topos_proxy"), default="h264",
        help="output codec; Topos proxy writes a native Topos 422 MOV",
    )
    parser.add_argument("--mode", choices=("sequential", "stack"),
                        default="sequential")
    parser.add_argument(
        "--blend-mode",
        choices=("normal", "multiply", "screen", "overlay", "darken",
                 "lighten", "color_dodge", "color_burn", "hard_light",
                 "soft_light", "difference", "exclusion", "hue",
                 "saturation", "color", "luminosity"),
        default="normal",
        help="top-layer blend mode for --mode stack",
    )
    parser.add_argument("--effect", choices=("none", "blur", "sharpen", "glow",
                                              "erode_dilate", "edge_detect",
                                              "denoise", "defocus", "chain"),
                        default="none")
    parser.add_argument("--effect-radius", type=float, default=None)
    parser.add_argument("--blur-type", choices=(
                            "box", "gaussian", "bilateral", "motion", "bokeh"),
                        default="gaussian")
    parser.add_argument("--edge-preserve", type=float, default=0.5)
    parser.add_argument("--edge-mode", choices=("extend", "reflect", "wrap"),
                        default="extend")
    parser.add_argument("--edge-kernel-size", type=int, choices=(3, 5, 7),
                        default=3)
    parser.add_argument("--defocus-mode", choices=("separable", "mipmap"),
                        default="separable")
    parser.add_argument(
        "--cpu-compositor", action="store_true",
        help="disable GPU composition and time the CPU compositor",
    )
    args = parser.parse_args()
    global EFFECT, EFFECT_RADIUS, EFFECT_BLUR_TYPE, EFFECT_EDGE_PRESERVE
    global EFFECT_EDGE_MODE, EFFECT_EDGE_KERNEL_SIZE
    global EFFECT_DEFOCUS_MODE, BLEND_MODE, GPU_RENDER_ENABLED, EXPORT_CODEC
    EFFECT = args.effect
    EFFECT_BLUR_TYPE = args.blur_type
    EFFECT_EDGE_PRESERVE = args.edge_preserve
    EFFECT_EDGE_MODE = args.edge_mode
    EFFECT_EDGE_KERNEL_SIZE = args.edge_kernel_size
    EFFECT_DEFOCUS_MODE = args.defocus_mode
    BLEND_MODE = args.blend_mode
    GPU_RENDER_ENABLED = not args.cpu_compositor
    EXPORT_CODEC = args.codec
    if args.mode != "stack" and BLEND_MODE != "normal":
        parser.error("--blend-mode other than normal requires --mode stack")
    default_radius = 1.0 if EFFECT == "sharpen" else 5.0
    EFFECT_RADIUS = (
        None if EFFECT in ("none", "chain", "erode_dilate", "edge_detect",
                           "denoise", "defocus")
        else max(
            0.1,
            float(args.effect_radius)
            if args.effect_radius is not None else default_radius,
        )
    )
    result = _run(args.backend, args.mode)
    json_path = Path(result["output_path"]).with_suffix(".json")
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
