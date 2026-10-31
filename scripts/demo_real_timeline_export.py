"""真实素材时间线导出演示：建工程 → 存 .topos → 重载校验 → 真实导出。

与 bench_timeline_export_windows.py 的差异：那个是协议化基准（多组对照、
测完删产物）；本脚本是**单次透明演示**——完整走一遍用户在产品里做的事：

1. 创建 EditProject 工程，把 tests/test_videos 的真实素材登记为媒体资产；
2. 创建时间线，把素材铺到视频轨上（默认 480 帧）；
3. 工程保存为 .topos 文件，再重新加载校验（工程文件真实可开）；
4. 经生产导出链路（TimelineExporter + FFmpegVideoEncoder，本机默认
   AMF 硬件编码）真实导出，实时打印帧率；
5. 产物保留在输出目录，附 ffprobe 信息。

用法：
    .venv/Scripts/python.exe -u scripts/demo_real_timeline_export.py
    .venv/Scripts/python.exe -u scripts/demo_real_timeline_export.py \
        --source "4K_H264.mov" --frames 240
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_VIDEOS_DIR = ROOT / "tests" / "test_videos"


def probe_source(path: Path) -> Dict[str, Any]:
    import av

    container = av.open(str(path))
    try:
        stream = next(s for s in container.streams if s.type == "video")
        ctx = stream.codec_context
        return {
            "codec": ctx.name if ctx else "?",
            "pix_fmt": ctx.pix_fmt if ctx else "?",
            "width": int(ctx.width or 0),
            "height": int(ctx.height or 0),
            "fps": float(stream.average_rate) if stream.average_rate else 25.0,
            "total_frames": int(stream.frames or 0),
        }
    finally:
        container.close()


def probe_output(path: Path) -> Dict[str, Any]:
    import av

    container = av.open(str(path))
    try:
        stream = next(s for s in container.streams if s.type == "video")
        ctx = stream.codec_context
        decoded = sum(1 for _ in container.decode(video=0))
        return {
            "codec": ctx.name if ctx else "?",
            "pix_fmt": ctx.pix_fmt if ctx else "?",
            "width": int(ctx.width or 0),
            "height": int(ctx.height or 0),
            "decoded_frames": decoded,
        }
    finally:
        container.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="2K_h264.mp4",
                        help="tests/test_videos 下的素材文件名")
    parser.add_argument("--frames", type=int, default=480,
                        help="时间线长度（帧）")
    parser.add_argument("--bitrate-mbps", type=int, default=20)
    parser.add_argument("--out-dir", default=str(ROOT / "bench_out" / "real_export_demo"))
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtWidgets import QApplication

        if QApplication.instance() is None:
            QApplication([])
    except Exception:
        pass  # 导出器不依赖 QApplication 时照常工作

    source = TEST_VIDEOS_DIR / args.source
    if not source.exists():
        print(f"❌ 素材不存在: {source}")
        return 1
    src_meta = probe_source(source)
    frames = args.frames
    if src_meta["total_frames"] and src_meta["total_frames"] < frames:
        print(f"❌ 素材只有 {src_meta['total_frames']} 帧，不足 {frames}")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    project_path = out_dir / f"demo_timeline_{source.stem}.topos"
    output_path = out_dir / f"demo_export_{source.stem}.mp4"

    print("=" * 64)
    print("真实素材时间线导出演示（生产导出链路）")
    print("=" * 64)
    print(f"素材: {source.name}")
    print(f"  codec={src_meta['codec']} pix_fmt={src_meta['pix_fmt']} "
          f"{src_meta['width']}x{src_meta['height']} "
          f"@ {src_meta['fps']:.3f}fps, 共 {src_meta['total_frames']} 帧")
    print(f"时间线: 单段视频轨 {frames} 帧（工程默认硬解下载 + 本机 AMF 硬编）")

    # --- 1/4 创建工程与时间线 ---------------------------------------------
    from src.features.edit.core.project import (
        EditProject,
        MediaAsset,
        TimelineSequenceRef,
    )
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportSource,
        TimelineExportConfig,
        create_timeline_exporter,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        Rational,
        TimelineSequence,
    )

    fps = src_meta["fps"]
    timeline_fps = max(1, int(round(fps)))
    asset = MediaAsset(
        asset_id="demo_asset", path=str(source), media_type="video",
        display_name=source.stem,
        metadata={"fps": fps, "total_frames": src_meta["total_frames"]},
    )
    project = EditProject.create(name="real-export-demo")
    project = project.with_media_asset_added(asset)

    clip = ClipInstance(
        clip_id="demo_clip", asset_id="demo_asset", track_id="v1",
        timeline_start=0, timeline_duration=frames,
        source_start=0, source_duration=frames,
    )
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(clip,),
    )
    sequence = TimelineSequence(
        timeline_id="demo", name="Demo Timeline",
        fps=Rational(timeline_fps, 1),
        width=src_meta["width"], height=src_meta["height"],
        video_tracks=(track,),
    )
    # 工程级登记时间线（保存校验要求 project.timelines 与序列表一致）
    project = project.with_timeline_ref_added(
        TimelineSequenceRef(
            timeline_id="demo", name="Demo Timeline",
            fps=float(timeline_fps),
            width=src_meta["width"], height=src_meta["height"],
            created_at=time.time(),
        ),
        set_active=True,
    )
    print(f"[1/4] ✅ 工程已创建：1 个媒体资产，1 条时间线 "
          f"({src_meta['width']}x{src_meta['height']} "
          f"@ {timeline_fps}fps, 1 个片段)")

    # --- 2/4 保存 .topos 工程文件 -----------------------------------------
    from src.features.edit.core.edit_project_io import EditProjectIO

    io = EditProjectIO()
    ok = io.save_project(str(project_path), project,
                         timeline_sequences={"demo": sequence})
    if not ok or not project_path.exists():
        print(f"[2/4] ❌ 工程保存失败: {project_path}")
        return 1
    size_kb = project_path.stat().st_size // 1024
    reloaded = io.load_project(str(project_path))
    reloaded_project, _ui, reloaded_sequences = reloaded
    n_assets = len(reloaded_project.assets)
    n_clips = sum(
        len(t.clips)
        for seq in (reloaded_sequences or {}).values()
        for t in seq.video_tracks
    )
    print(f"[2/4] ✅ 工程已保存并重新加载校验: {project_path.name} "
          f"({size_kb}KB, 资产 {n_assets} 个, 重载片段 {n_clips} 个)")

    # --- 3/4 真实导出（实时帧率） ------------------------------------------
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="demo",
        output_path=str(output_path),
        codec=ExportCodec.H264,
        quality=args.bitrate_mbps * 1_000_000,
        quality_mode="vbr",
        include_audio=False,
    )
    started = time.perf_counter()
    last_report = {"frame": -1}

    def on_progress(current: int, total: int, _percent: float) -> None:
        if current - last_report["frame"] >= 60 or current == total:
            last_report["frame"] = current
            elapsed = time.perf_counter() - started
            fps_now = current / elapsed if elapsed > 0 else 0.0
            print(f"[3/4] 导出中 {current}/{total} 帧 | "
                  f"已用 {elapsed:5.1f}s | 当前 {fps_now:6.2f} fps",
                  flush=True)

    exporter = create_timeline_exporter(
        project, sequence, progress_callback=on_progress,
    )

    outcome = exporter.export_timeline(config)
    wall_s = time.perf_counter() - started
    if not outcome.success:
        print(f"[3/4] ❌ 导出失败: error_code={outcome.error_code} "
              f"cause={outcome.cause[:300]}")
        return 1

    avg_fps = frames / wall_s if wall_s > 0 else 0.0
    print(f"[3/4] ✅ 导出完成: {output_path.name}")
    print(f"      平均帧率 {avg_fps:.2f} fps（{frames} 帧 / {wall_s:.1f}s）"
          f"  输出 {output_path.stat().st_size / (1024*1024):.1f} MB")
    if outcome.hw_fallback_reason:
        print(f"      硬编回退: {outcome.hw_fallback_reason}")
    else:
        print("      硬件编码已启用（无回退）")

    # --- 4/4 输出校验 -------------------------------------------------------
    out_meta = probe_output(output_path)
    frames_ok = out_meta["decoded_frames"] == frames
    print(f"[4/4] {'✅' if frames_ok else '❌'} 输出校验: "
          f"{out_meta['codec']}/{out_meta['pix_fmt']} "
          f"{out_meta['width']}x{out_meta['height']}, "
          f"解码 {out_meta['decoded_frames']}/{frames} 帧")
    print("=" * 64)
    print(f"结论：真实素材 {source.name} → 时间线 {frames} 帧 → "
          f"生产链路导出 {avg_fps:.2f} fps")
    print(f"产物：{output_path}")
    print(f"工程：{project_path}")
    return 0 if frames_ok else 1


if __name__ == "__main__":
    sys.exit(main())
