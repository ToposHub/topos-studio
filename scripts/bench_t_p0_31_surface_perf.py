"""T-P0-31 卡验证 4：TOPOS 422 → NV12 surface → VideoToolbox H.264 性能归因。

跑与 tests/edit/test_native_gpu_bridge_eligibility.py 的
TestRealTopos422GpuBridge 相同的真实链路（默认 48 帧），但目标是
**每阶段 wall p50/p95**——回答 ≥60 FPS 缺口在 decode / render /
encode_submit 哪一段。证据 JSON 落盘 bench_out/ai_upgrade/T-P0-31_*。

用法：
  TOPOS_CODEC_LIB=native/topos_codec/build/release/topos_codec.0.1.0.dylib \
      .venv/bin/python scripts/bench_t_p0_31_surface_perf.py [--frames 48]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("TOPOS_EXPORT_NATIVE_VT", "1")
os.environ.setdefault("TOPOS_EXPORT_GPU_RENDER", "1")

sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--codec", choices=("h264", "hevc"), default="h264")
    parser.add_argument("--pixfmt", default=None,
                        help="默认 h264→yuv420p(nv12 surface)、hevc→yuv420p10le(p010)")
    parser.add_argument("--runs", type=int, default=1,
                        help="重复次数（取各次 p50 中位数，观察抖动）")
    args = parser.parse_args()

    frames = args.frames
    if args.pixfmt:
        pix_fmt = args.pixfmt
    else:
        pix_fmt = "yuv420p10le" if args.codec == "hevc" else "yuv420p"
    container = "mov" if args.codec == "hevc" else "mp4"

    from src.features.edit.core.project import (
        EditProject,
        MediaAsset,
        probe_media_metadata,
    )
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportRangeType,
        ExportSource,
        TimelineExportConfig,
        create_timeline_exporter,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        TimelineSequence,
    )
    from src.shared.media.metadata import Rational

    source_path = ROOT / "tests/test_videos/2K_topos_proxy.mov"
    if not source_path.is_file():
        print(f"missing source: {source_path}")
        return 2

    # 源素材实际帧数（2K_topos_proxy.mov = 96 帧）。时间线超出源长会
    # 触发越界解码 → CPU 回落 → bridge 拒收——那是配置错误不是被测路径。
    # 更长的稳态测量用多片段背靠背拼接（循环同一源）。
    from src.shared.media.factory import MediaSourceFactory
    probe = MediaSourceFactory.probe_video_metadata_cached(str(source_path))
    source_frames = int(getattr(probe.metadata, "total_frames", 0) or 0)
    if source_frames <= 0:
        print("cannot determine source frame count")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "bench_out" / "ai_upgrade" / f"T-P0-31_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = []
    for run_index in range(args.runs):
        output = out_dir / f"out_{args.codec}_{frames}f_r{run_index}.{container}"
        project = EditProject.create(name="t-p0-31-perf")
        metadata = {"fps": 25.0, "total_frames": frames}
        metadata.update(probe_media_metadata(str(source_path)))
        metadata["total_frames"] = source_frames
        asset = MediaAsset(
            asset_id="asset-perf", path=str(source_path),
            media_type="video", display_name="perf",
            metadata=metadata,
        )
        project = project.with_media_asset_added(asset)
        # 背靠背拼接 clip 序列：每段 ≤ 源长，凑满 frames 总时长。
        clips = []
        remaining = frames
        timeline_start = 0
        clip_index = 0
        while remaining > 0:
            take = min(source_frames, remaining)
            clips.append(ClipInstance(
                clip_id=f"clip-perf-{clip_index}",
                asset_id=asset.asset_id,
                track_id="v1", timeline_start=timeline_start,
                timeline_duration=take, source_start=0,
                source_duration=take,
            ))
            remaining -= take
            timeline_start += take
            clip_index += 1
        v1 = EditTrack(track_id="v1", name="V1", track_type="video",
                       index=0, clips=tuple(clips))
        sequence = TimelineSequence(
            timeline_id="tl-perf", name="Perf",
            fps=Rational(25, 1), width=1920, height=1080,
            video_tracks=(v1,), audio_tracks=(),
        )
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id=sequence.timeline_id,
            range_type=ExportRangeType.ENTIRE,
            codec=ExportCodec.H264 if args.codec == "h264" else ExportCodec.HEVC,
            pixel_format=pix_fmt,
            include_audio=False,
            output_path=str(output),
        )
        exporter = create_timeline_exporter(project=project,
                                            sequence=sequence)
        t0 = time.perf_counter()
        outcome = exporter.export_timeline(config)
        elapsed = time.perf_counter() - t0

        telemetry = outcome.stage_telemetry or {}
        stages = {}
        for s in telemetry.get("stages", []):
            stages[s["name"]] = {
                "backend": s.get("backend"),
                "sample_count": s.get("sample_count"),
                "wall_p50_ms": s.get("wall_p50_ms"),
                "wall_p95_ms": s.get("wall_p95_ms"),
                "gpu_submit_p50_ms": s.get("gpu_submit_p50_ms"),
                "gpu_submit_p95_ms": s.get("gpu_submit_p95_ms"),
            }
        # 每帧墙钟（首帧含解码器/GOP 冷启动，单列）
        frame_ms = telemetry.get("frame_attributions", [])

        probe = {}
        try:
            import av
            with av.open(str(output)) as c:
                stream = c.streams.video[0]
                probe = {
                    "codec": stream.codec_context.name,
                    "pix_fmt": str(stream.codec_context.pix_fmt),
                    "frames": sum(1 for _ in c.decode(stream)),
                }
        except Exception as exc:  # noqa: BLE001
            probe = {"error": repr(exc)}

        record = {
            "run": run_index,
            "success": bool(outcome.success),
            "cause": outcome.cause,
            "elapsed_s": round(elapsed, 3),
            "fps": round(frames / elapsed, 2) if elapsed > 0 else None,
            "delivery_path": telemetry.get("delivery_path", ""),
            "full_gpu_blockers": telemetry.get("full_gpu_blockers"),
            "fallback_root_cause": telemetry.get("fallback_root_cause"),
            "cpu_upload_bytes": telemetry.get("cpu_upload_bytes"),
            "cpu_download_bytes": telemetry.get("cpu_download_bytes"),
            "bridge_selection": getattr(exporter, "bridge_selection", None),
            "stages": stages,
            "probe": probe,
            "output": str(output),
        }
        runs.append(record)
        print(f"[run {run_index}] ok={record['success']} "
              f"fps={record['fps']} elapsed={record['elapsed_s']}s")
        for name, s in sorted(stages.items()):
            print(f"    {name:16s} backend={s['backend']!r:<18} "
                  f"n={s['sample_count']} p50={s['wall_p50_ms']}ms "
                  f"p95={s['wall_p95_ms']}ms")

    result = {
        "schema": "t-p0-31-surface-perf/1",
        "date": stamp,
        "frames": frames,
        "codec": args.codec,
        "pixel_format": pix_fmt,
        "runs": runs,
    }
    (out_dir / "perf.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"evidence → {out_dir / 'perf.json'}")

    ok_runs = [r for r in runs if r["success"]]
    return 0 if ok_runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
