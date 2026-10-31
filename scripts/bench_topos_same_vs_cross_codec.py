"""同族 vs 跨族编码导出速度基准：时间线单一 topos 素材 → 四种输出编码。

回答的问题：时间线素材是 topos 编码（如 Topos 422 HQ）时，
  1) 导出同为 topos 编码（如 Topos 422 Proxy）的真实速度是多少？
  2) 同族（topos→topos）是否显著快于跨族（topos→ProRes/H264）？

方法（与 scripts/demo_real_timeline_export.py 同一生产链路）：
- 对每个输出编码，用**完全相同的时间线**（单一 clip 全覆盖导出范围，
  无特效/几何/alpha/音频）各跑 N 次；
- topos→topos(422 10bit) 若满足 identity 条件会走 T-P1-10
  ``topos_direct_planar_cpu_encode`` 直通路径（outcome.delivery_path 可验证）；
- 记录整段平均 fps 与 25%→100% 段落 fps（剥离启动开销），
  输出产物校验帧数后保留。

用法：
    .venv/bin/python -u scripts/bench_topos_same_vs_cross_codec.py
    .venv/bin/python -u scripts/bench_topos_same_vs_cross_codec.py \
        --source 4K_topos_hq.mov --reps 3
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_VIDEOS_DIR = ROOT / "tests" / "test_videos"


def probe_source(path: Path) -> Dict[str, Any]:
    """优先用工程 probe（topos 文件 PyAV 不识别），回退 PyAV。"""
    from src.shared.media.factory import MediaSourceFactory

    probe = MediaSourceFactory.probe_video_metadata_cached(str(path))
    vm = probe.metadata
    if vm is not None:
        fps = getattr(vm, "fps", None)
        fps_f = float(fps.num) / float(fps.den) if fps else 25.0
        return {
            "codec": vm.codec_name, "pix_fmt": vm.pixel_format,
            "width": int(vm.width), "height": int(vm.height),
            "fps": fps_f, "total_frames": int(vm.total_frames or 0),
        }
    import av

    container = av.open(str(path))
    try:
        s = next(s for s in container.streams if s.type == "video")
        ctx = s.codec_context
        return {
            "codec": ctx.name if ctx else "?",
            "pix_fmt": str(ctx.pix_fmt) if ctx else "?",
            "width": int(ctx.width or 0), "height": int(ctx.height or 0),
            "fps": float(s.average_rate) if s.average_rate else 25.0,
            "total_frames": int(s.frames or 0),
        }
    finally:
        container.close()


def verify_output(path: Path, expect_frames: int) -> Dict[str, Any]:
    """校验输出帧数/编码。topos 输出走工程 probe，其余走 PyAV 解码计数。"""
    from src.shared.media.factory import MediaSourceFactory

    probe = MediaSourceFactory.probe_video_metadata_cached(str(path))
    vm = probe.metadata
    if vm is not None and vm.codec_name == "topos":
        fps = getattr(vm, "fps", None)
        return {
            "codec": vm.codec_name, "pix_fmt": vm.pixel_format,
            "decoded_frames": int(vm.total_frames or 0),
            "fps": float(fps.num) / float(fps.den) if fps else 0.0,
        }
    import av

    container = av.open(str(path))
    try:
        s = next(s for s in container.streams if s.type == "video")
        ctx = s.codec_context
        decoded = sum(1 for _ in container.decode(video=0))
        return {
            "codec": ctx.name if ctx else "?",
            "pix_fmt": str(ctx.pix_fmt) if ctx else "?",
            "decoded_frames": decoded, "fps": 0.0,
        }
    finally:
        container.close()


# (名称, ExportCodec 值, quality, quality_mode, 扩展名, 说明)
MATRIX = [
    ("topos_proxy", "topos_proxy", None, "auto", "mov",
     "同族：Topos 422 HQ → Topos 422 Proxy（档位下调）"),
    ("topos_hq", "topos_hq", None, "auto", "mov",
     "同族同档：Topos 422 HQ → Topos 422 HQ（对照）"),
    ("prores_422", "prores_422", None, "auto", "mov",
     "跨族 CPU：→ ProRes 422"),
    ("h264", "h264", 20_000_000, "vbr", "mp4",
     "跨族硬编：→ H.264 (20 Mbps VBR)"),
]


def build_timeline(source: Path, meta: Dict[str, Any], frames: int):
    from src.features.edit.core.project import (
        EditProject, MediaAsset, TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, TimelineSequence,
    )

    fps = meta["fps"]
    timeline_fps = max(1, int(round(fps)))
    asset = MediaAsset(
        asset_id="bench_asset", path=str(source), media_type="video",
        display_name=source.stem,
        metadata={"fps": fps, "total_frames": meta["total_frames"]},
    )
    project = EditProject.create(name="topos-vs-cross-bench")
    project = project.with_media_asset_added(asset)
    clip = ClipInstance(
        clip_id="bench_clip", asset_id="bench_asset", track_id="v1",
        timeline_start=0, timeline_duration=frames,
        source_start=0, source_duration=frames,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    sequence = TimelineSequence(
        timeline_id="bench", name="Bench Timeline",
        fps=Rational(timeline_fps, 1),
        width=meta["width"], height=meta["height"],
        video_tracks=(track,),
    )
    project = project.with_timeline_ref_added(
        TimelineSequenceRef(
            timeline_id="bench", name="Bench Timeline",
            fps=float(timeline_fps), width=meta["width"],
            height=meta["height"], created_at=time.time(),
        ),
        set_active=True,
    )
    return project, sequence


def run_once(codec_entry, project, sequence, frames: int, out_path: Path):
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportSource, TimelineExportConfig,
        create_timeline_exporter,
    )

    name, codec_val, quality, quality_mode, _ext, _desc = codec_entry
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="bench",
        output_path=str(out_path),
        codec=ExportCodec(codec_val),
        quality=quality,
        quality_mode=quality_mode,
        include_audio=False,
    )
    progress: List[tuple] = []
    t0 = time.perf_counter()

    def on_progress(current: int, total: int, _pct: float) -> None:
        progress.append((current, time.perf_counter() - t0))

    exporter = create_timeline_exporter(
        project, sequence, progress_callback=on_progress)
    outcome = exporter.export_timeline(config)
    wall_s = time.perf_counter() - t0
    if not outcome.success:
        return {
            "ok": False, "wall_s": wall_s,
            "error_code": outcome.error_code,
            "cause": (outcome.cause or "")[:300],
        }

    # 整段 fps 与 25%→100% 段 fps（剥离启动开销）
    overall_fps = frames / wall_s if wall_s > 0 else 0.0
    seg_fps = None
    if len(progress) >= 4:
        quarter_idx = max(0, int(len(progress) * 0.25) - 1)
        c0, t_head = progress[quarter_idx]
        c1, t_end = progress[-1]
        if c1 > c0 and t_end > t_head:
            seg_fps = (c1 - c0) / (t_end - t_head)

    return {
        "ok": True, "wall_s": wall_s, "overall_fps": overall_fps,
        "segment_fps": seg_fps, "delivery_path": outcome.delivery_path,
        "hw_fallback_reason": outcome.hw_fallback_reason or "",
        "output_bytes": out_path.stat().st_size if out_path.exists() else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="2K_topos_hq.mov",
                        help="tests/test_videos 下的 topos 素材")
    parser.add_argument("--frames", type=int, default=0,
                        help="时间线帧数（0 = 素材全部帧）")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument(
        "--only", default="",
        help="只跑矩阵中匹配名称的项（逗号分隔，如 topos_proxy,h264）")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtWidgets import QApplication

        if QApplication.instance() is None:
            QApplication([])
    except Exception:
        pass

    source = TEST_VIDEOS_DIR / args.source
    if not source.exists():
        print(f"❌ 素材不存在: {source}")
        return 1
    meta = probe_source(source)
    frames = args.frames or meta["total_frames"]
    frames = min(frames, meta["total_frames"])
    matrix = [m for m in MATRIX
              if not args.only or m[0] in args.only.split(",")]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else (
        ROOT / "bench_out" / "topos_same_vs_cross" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("时间线导出速度基准：同族（topos→topos） vs 跨族编码")
    print("=" * 72)
    print(f"素材: {source.name}  codec={meta['codec']} "
          f"pix_fmt={meta['pix_fmt']} {meta['width']}x{meta['height']} "
          f"@ {meta['fps']:.2f}fps  时间线 {frames} 帧 × {args.reps} 次")
    print(f"输出目录: {out_dir}")
    print()

    results: Dict[str, Any] = {
        "source": str(source), "source_meta": meta, "frames": frames,
        "reps": args.reps, "matrix": [],
    }
    for entry in matrix:
        name, codec_val, _q, _qm, ext, desc = entry
        rows: List[Dict[str, Any]] = []
        for rep in range(args.reps):
            out_path = out_dir / f"out_{name}_rep{rep}.{ext}"
            project, sequence = build_timeline(source, meta, frames)
            r = run_once(entry, project, sequence, frames, out_path)
            r["rep"] = rep
            rows.append(r)
            if not r["ok"]:
                print(f"  [{name}] rep{rep}: ❌ {r['error_code']} "
                      f"{r.get('cause', '')}")
                continue
            seg = (f"{r['segment_fps']:.2f} fps"
                   if r["segment_fps"] else "n/a")
            print(f"  [{name}] rep{rep}: {r['wall_s']:6.2f}s  "
                  f"整体 {r['overall_fps']:7.2f} fps  稳态 {seg}  "
                  f"path={r['delivery_path'] or 'render_plan'}"
                  f"{'  hw_fallback=' + r['hw_fallback_reason'] if r['hw_fallback_reason'] else ''}")
        oks = [r for r in rows if r.get("ok")]
        summary = {
            "name": name, "codec": codec_val, "desc": desc,
            "runs": rows,
        }
        if oks:
            of = [r["overall_fps"] for r in oks]
            sf = [r["segment_fps"] for r in oks if r["segment_fps"]]
            v = verify_output(
                out_dir / f"out_{name}_rep{len(oks)-1}.{ext}", frames)
            summary.update({
                "overall_fps_median": statistics.median(of),
                "overall_fps_max": max(of),
                "segment_fps_median": statistics.median(sf) if sf else None,
                "verify": v,
                "frames_ok": v["decoded_frames"] == frames,
            })
            print(f"  ▶ {name}: 整体中位 {summary['overall_fps_median']:.2f} fps"
                  f"（峰值 {summary['overall_fps_max']:.2f}）"
                  f"  稳态中位 "
                  f"{summary['segment_fps_median'] and round(summary['segment_fps_median'], 2)} fps"
                  f"  输出 {v['codec']}/{v['pix_fmt']} "
                  f"{v['decoded_frames']}/{frames} 帧")
        print(f"  ({desc})")
        print("-" * 72)
        results["matrix"].append(summary)

    # 汇总对比
    print()
    print("结论（整体帧率中位数，fps）：")
    base = next((m for m in results["matrix"]
                 if m["name"] == "topos_proxy"), None)
    for m in results["matrix"]:
        if "overall_fps_median" not in m:
            print(f"  {m['name']:12s}: 失败")
            continue
        line = f"  {m['name']:12s}: {m['overall_fps_median']:8.2f} fps"
        if base and "overall_fps_median" in base and m is not base:
            line += f"  （为 topos_proxy 的 {m['overall_fps_median'] / base['overall_fps_median']:5.2f}×）"
        print(line)

    result_path = out_dir / "results.json"
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n结果已保存: {result_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
