#!/usr/bin/env python3
"""Run the explicit Metal-to-VideoToolbox timeline bridge and save evidence.

This benchmark is intentionally scoped to M5's current proof boundary:

    VideoToolbox H.264 frame -> Metal NV12 render -> VideoToolbox H.264 MP4

It does not turn missing GPU timestamps into estimates.  The JSON report keeps
the ExportGpuTrail claim fail-closed when the decoder or VideoToolbox submitter
does not expose a real GPU timestamp.

Example::

    TOPOS_EXPORT_GPU_RENDER=1 .venv/bin/python \
        tools/bench_macos_gpu_vt_export.py --frames 480
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "tests" / "test_videos" / "2K_h264.mp4"
DEFAULT_REPORT = ROOT / "tests" / "media" / "results" / "macos_gpu_export_20260915.json"
DEFAULT_MARKDOWN = ROOT / "tests" / "media" / "results" / "macos_gpu_export_20260915.md"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip()
    except Exception:
        return "unknown"


def _probe_source(path: Path) -> dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        stream = next(item for item in container.streams if item.type == "video")
        codec = stream.codec_context
        return {
            "codec": str(codec.name),
            "pixel_format": str(codec.pix_fmt),
            "width": int(codec.width or 0),
            "height": int(codec.height or 0),
            "fps": float(stream.average_rate) if stream.average_rate else 0.0,
            "container_frame_count": int(stream.frames or 0),
        }


def _probe_output(path: Path) -> dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        stream = next(item for item in container.streams if item.type == "video")
        codec = stream.codec_context
        decoded_frames = sum(1 for _ in container.decode(video=0))
        return {
            "codec": str(codec.name),
            "pixel_format": str(codec.pix_fmt),
            "width": int(codec.width or 0),
            "height": int(codec.height or 0),
            "decoded_frames": decoded_frames,
            "container_frame_count": int(stream.frames or 0),
        }


def _build_timeline(source: Path, frames: int, fps: int):
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.shared.media.metadata import Rational

    metadata = _probe_source(source)
    project = EditProject.create("macOS GPU VT benchmark")
    project = project.import_asset(
        path=str(source),
        media_type="video",
        metadata={
            "width": metadata["width"],
            "height": metadata["height"],
            "fps": float(fps),
            "total_frames": frames,
            "duration_seconds": frames / float(fps),
        },
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="macos-gpu-vt-clip",
        asset_id=asset_id,
        track_id="macos-gpu-vt-video",
        timeline_start=0,
        timeline_duration=frames,
        source_start=0,
        source_duration=frames,
    )
    sequence = TimelineSequence(
        name="macOS GPU VT benchmark",
        width=metadata["width"],
        height=metadata["height"],
        fps=Rational(fps, 1),
        video_tracks=(
            EditTrack(
                track_id="macos-gpu-vt-video",
                name="Video 1",
                track_type="video",
                index=0,
                clips=(clip,),
            ),
        ),
    )
    return project, sequence, metadata


def _markdown(report: dict[str, Any]) -> str:
    trail = report["gpu_trail"]
    lines = [
        "# macOS Metal → VideoToolbox timeline export benchmark",
        "",
        f"- Status: `{report['status']}`",
        f"- Generated: `{report['generated_at_utc']}`",
        f"- Commit: `{report['git_commit']}`",
        f"- Full-GPU claim: `{report['full_gpu_claim']}`",
        f"- Claim reason: `{report['full_gpu_claim_reason']}`",
        "",
        "## Scope",
        "",
        "Explicit `VideoToolboxGpuExportBridge` through `TimelineExporter`; "
        "single H.264 video track, no audio, no subtitles, Metal NV12 output.",
        "",
        "## Input / output",
        "",
        f"- Input: `{report['input']['path']}`",
        f"- Input SHA-256: `{report['input']['sha256']}`",
        f"- Input: `{report['input']['codec']}` / `{report['input']['pixel_format']}` "
        f"`{report['input']['width']}x{report['input']['height']}` @ "
        f"`{report['input']['fps']:.3f}` fps",
        f"- Requested frames: `{report['timeline']['requested_frames']}`",
        f"- Output: `{report['output']['path']}`",
        f"- Output: `{report['output']['codec']}` / `{report['output']['pixel_format']}` "
        f"`{report['output']['width']}x{report['output']['height']}`, "
        f"decoded `{report['output']['decoded_frames']}` frames",
        f"- Native VT packets: `{report['output']['native_vt_packet_count']}`",
        "",
        "## Timing",
        "",
        f"- Wall time: `{report['timing']['wall_seconds']:.3f}` s",
        f"- End-to-end throughput: `{report['timing']['fps']:.3f}` fps",
        "",
        "## ExportGpuTrail",
        "",
        "| Stage | Backend | Samples | Mean GPU ms | P50 GPU ms | P95 GPU ms | CPU upload | CPU download |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in trail["stages"]:
        lines.append(
            f"| `{stage['name']}` | `{stage['backend']}` | "
            f"{stage['sample_count']} | {stage['gpu_submit_ms']!s} | "
            f"{stage['gpu_submit_p50_ms']!s} | {stage['gpu_submit_p95_ms']!s} | "
            f"{stage['cpu_upload_bytes']} | {stage['cpu_download_bytes']} |"
        )
    lines.extend(
        [
            "",
            f"- Trail CPU upload bytes: `{trail['cpu_upload_bytes']}`",
            f"- Trail CPU download bytes: `{trail['cpu_download_bytes']}`",
            f"- Fallback reason: `{trail['fallback_reason']}`",
            "",
            "## Boundary",
            "",
            "This report proves the real multi-frame bridge and playable output. "
            "It does not claim full-chain GPU completion when decode or native "
            "VideoToolbox encode timestamps are unavailable; the claim remains "
            "fail-closed in the JSON evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--frames", type=int, default=480)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This benchmark requires macOS VideoToolbox", file=sys.stderr)
        return 2
    if args.frames < 1 or args.fps < 1:
        print("--frames and --fps must be positive", file=sys.stderr)
        return 2
    source = args.source if args.source.is_absolute() else ROOT / args.source
    if not source.is_file():
        print(f"Input does not exist: {source}", file=sys.stderr)
        return 2

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    output = args.output or (ROOT / "bench_out" / "macos_gpu_export_20260915" /
                             f"{source.stem}_{args.frames}frames.mp4")
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)

    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        TimelineExporter,
    )
    from src.shared.gpu.metal.context import MetalComputeContext
    from src.shared.media.macos_vt.gpu_export_bridge import (
        VideoToolboxGpuExportBridge,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    project, sequence, input_meta = _build_timeline(source, args.frames, args.fps)
    started = time.perf_counter()
    bridge = None
    exporter = None
    try:
        context = MetalComputeContext()
        bridge = VideoToolboxGpuExportBridge(
            context,
            str(output),
            width=input_meta["width"],
            height=input_meta["height"],
            fps=args.fps,
        )
        exporter = TimelineExporter(
            project=project,
            sequence=sequence,
            frame_provider=VideoFrameProviderService(),
            gpu_encode_surface_provider=bridge,
        )
        outcome = exporter.export_timeline(
            TimelineExportConfig(
                source=ExportSource.TIMELINE,
                timeline_id=sequence.timeline_id,
                range_type=ExportRangeType.ENTIRE,
                codec=ExportCodec.H264,
                output_path=str(output),
                resolution=(input_meta["width"], input_meta["height"]),
                fps=float(args.fps),
                include_audio=False,
                burn_subtitles=False,
            )
        )
        wall_seconds = time.perf_counter() - started
        output_meta = _probe_output(output) if output.is_file() else {}
        trail_snapshot = exporter.gpu_trail_snapshot
        if trail_snapshot is None:
            raise RuntimeError("TimelineExporter did not produce ExportGpuTrail")
        trail = trail_snapshot.to_dict()
        claim_reason = trail_snapshot.full_gpu_claim_reason()
        report = {
            "schema_version": 1,
            "probe": "macos_gpu_vt_timeline_export",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(),
            "status": "passed" if outcome.success and output_meta.get("decoded_frames") == args.frames else "failed",
            "full_gpu_claim": claim_reason == "",
            "full_gpu_claim_reason": claim_reason,
            "input": {"path": str(source), "sha256": _sha256(source), **input_meta},
            "timeline": {
                "requested_frames": args.frames,
                "fps": args.fps,
                "video_tracks": 1,
                "audio": False,
                "subtitles": False,
            },
            "output": {
                "path": str(output),
                "bytes": output.stat().st_size if output.is_file() else 0,
                "native_vt_packet_count": bridge.packet_count,
                **output_meta,
            },
            "timing": {
                "wall_seconds": wall_seconds,
                "fps": args.frames / wall_seconds if wall_seconds else 0.0,
            },
            "gpu_trail": trail,
            "outcome": {
                "success": bool(outcome.success),
                "error_code": str(outcome.error_code),
                "cause": str(outcome.cause or ""),
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "machine": platform.machine(),
                "metal_device": context.device_name,
            },
        }
    except Exception as exc:
        wall_seconds = time.perf_counter() - started
        report = {
            "schema_version": 1,
            "probe": "macos_gpu_vt_timeline_export",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(),
            "status": "failed",
            "full_gpu_claim": False,
            "full_gpu_claim_reason": "benchmark_error",
            "input": {"path": str(source), "sha256": _sha256(source), **input_meta},
            "timeline": {"requested_frames": args.frames, "fps": args.fps},
            "output": {"path": str(output)},
            "timing": {"wall_seconds": wall_seconds},
            "error": repr(exc),
        }
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        raise
    finally:
        if bridge is not None:
            bridge.close()

    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    args.markdown.write_text(_markdown(report))
    print(json.dumps({
        "status": report["status"],
        "frames": report["output"].get("decoded_frames", 0),
        "packets": report["output"].get("native_vt_packet_count", 0),
        "fps": report["timing"].get("fps", 0.0),
        "full_gpu_claim": report["full_gpu_claim"],
        "claim_reason": report["full_gpu_claim_reason"],
        "report": str(args.report),
    }, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
