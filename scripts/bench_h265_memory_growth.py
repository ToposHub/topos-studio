"""T-P1-71：H265/HEVC 连续导出 native 内存增长测量（X-TEST-50 口径）。

同一进程内连续跑 N 次 HEVC 导出（真实 TOPOS 422 源 → 1080p 交付），
每次导出后采样 RSS / 句柄数 / 残留临时文件。泄漏判据用线性拟合斜率：
无界增长 = RSS 随导出次数持续线性上升；有界缓存（池/缓存上限）会在
若干轮后平台化。证据 JSON 落盘 bench_out/ai_upgrade/T-P1-71_*。

用法：
  TOPOS_CODEC_LIB=native/topos_codec/build/release/topos_codec.0.1.0.dylib \
      .venv/bin/python scripts/bench_h265_memory_growth.py [--rounds 10]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _rss_mb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / (1024 * 1024)


def _handle_count() -> int:
    import psutil

    return psutil.Process().num_fds()


def _run_one_export(source_path: Path, frames: int, out: Path,
                    codec_name: str = "hevc") -> dict:
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

    project = EditProject.create(name="h265-mem")
    metadata = {"fps": 25.0, "total_frames": frames}
    metadata.update(probe_media_metadata(str(source_path)))
    metadata["total_frames"] = frames
    project = project.with_media_asset_added(MediaAsset(
        asset_id="a", path=str(source_path), media_type="video",
        display_name="mem", metadata=metadata))
    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v1", timeline_start=0,
        timeline_duration=frames, source_start=0, source_duration=frames)
    v1 = EditTrack(track_id="v1", name="V1", track_type="video",
                   index=0, clips=(clip,))
    seq = TimelineSequence(timeline_id="tl", name="Mem",
                           fps=Rational(25, 1), width=1920, height=1080,
                           video_tracks=(v1,), audio_tracks=())
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id=seq.timeline_id,
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.HEVC if codec_name == "hevc" else ExportCodec.H264,
        pixel_format="yuv420p",
        include_audio=False,
        output_path=str(out),
    )
    exporter = create_timeline_exporter(project=project, sequence=seq)
    t0 = time.perf_counter()
    outcome = exporter.export_timeline(config)
    return {
        "success": bool(outcome.success),
        "cause": str(outcome.cause or ""),
        "wall_s": round(time.perf_counter() - t0, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--codec", choices=("hevc", "h264"), default="hevc",
                        help="h264 臂作对照：两者都增长 → 解码/框架侧；仅 hevc → 编码器侧")
    args = parser.parse_args()

    from src.shared.media.factory import MediaSourceFactory

    source_path = ROOT / "tests/test_videos/2K_topos_proxy.mov"
    probe = MediaSourceFactory.probe_video_metadata_cached(str(source_path))
    source_frames = int(getattr(probe.metadata, "total_frames", 0) or 0)
    frames = min(args.frames, source_frames) or args.frames

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "bench_out" / "ai_upgrade" / f"T-P1-71_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 预热一次：一次性初始化（解码器/编码器/Metal 管线）不应算进泄漏斜率
    warm_out = out_dir / "warm.mp4"
    warm = _run_one_export(source_path, frames, warm_out, args.codec)
    warm_out.unlink(missing_ok=True)
    gc.collect()
    base_rss = _rss_mb()

    samples = []
    for i in range(args.rounds):
        out = out_dir / f"round_{i:02d}.mp4"
        rec = _run_one_export(source_path, frames, out, args.codec)
        gc.collect()
        time.sleep(0.3)  # 给后台线程（manifest 等）收尾窗口
        rss = _rss_mb()
        handles = _handle_count()
        sidecars = list(out_dir.glob("*.tmp"))
        samples.append({
            "round": i,
            "success": rec["success"],
            "cause": rec["cause"],
            "wall_s": rec["wall_s"],
            "rss_mb": round(rss, 1),
            "delta_mb": round(rss - base_rss, 1),
            "handles": handles,
            "leftover_tmp": len(sidecars),
        })
        print(f"[{i:02d}] ok={rec['success']} rss={rss:.1f}MB "
              f"delta={rss - base_rss:+.1f} handles={handles}")
        out.unlink(missing_ok=True)

    # 线性拟合 delta_mb = a * round + b（最小二乘）
    n = len(samples)
    xs = [s["round"] for s in samples]
    ys = [s["delta_mb"] for s in samples]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    slope = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
             / sum((x - mean_x) ** 2 for x in xs)) if n > 1 else 0.0

    growth_unbounded = slope > 5.0  # MB/round：>5MB/轮视为可疑增长
    result = {
        "schema": "t-p1-71-h265-memory-growth/1",
        "date": stamp,
        "codec": args.codec,
        "rounds": args.rounds,
        "frames_per_round": frames,
        "warmup": {"success": warm["success"], "wall_s": warm["wall_s"]},
        "base_rss_mb": round(base_rss, 1),
        "slope_mb_per_round": round(slope, 3),
        "growth_unbounded_threshold": "5 MB/round",
        "growth_unbounded": growth_unbounded,
        "samples": samples,
    }
    (out_dir / "memory_growth.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"slope={slope:.3f} MB/round → "
          f"{'UNBOUNDED?' if growth_unbounded else 'bounded/plateau'}")
    print(f"evidence → {out_dir / 'memory_growth.json'}")
    return 0 if all(s["success"] for s in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
