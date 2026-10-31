#!/usr/bin/env python3
"""Deliver 导出端到端基线基准（P4 立项前置，总计划 §10.1/§16.1）。

对真实素材走**产品真实导出链**（TimelineExporter.export_timeline），
按 codec 档位测量端到端导出帧率，产出 JSON 到 bench_out/export_baseline/。

- 场景：单视频轨、真实测试素材、ENTIRE 范围（可用 --frames 截断）。
- 口径：wall 时间 / 成功编码帧数；不与任何预估比较。
- 用法：
    python3 tools/bench_export_baseline.py                # 默认矩阵
    python3 tools/bench_export_baseline.py --frames 96    # 更长采样
    python3 tools/bench_export_baseline.py --source 4K_H265.mov

真实素材门：素材缺失时跳过对应矩阵项（不伪造数据）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT))

from src.features.edit.core.project import EditProject, MediaAsset  # noqa: E402
from src.features.edit.core.timeline_export import (  # noqa: E402
    ExportCodec,
    ExportRangeType,
    ExportSource,
    TimelineExportConfig,
    create_timeline_exporter,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational  # noqa: E402

VIDEO_DIR = TEST_ROOT / "tests" / "test_videos"

# 基线矩阵：交付代表性档位（quick/中间/母版各至少一项）。
CODEC_MATRIX = [
    ("h264_yuv420p", ExportCodec.H264, None),
    ("prores_422", ExportCodec.PRORES_422, None),
    ("dnxhr_lb", ExportCodec.DNXHR_LB, None),
    ("topos_proxy", ExportCodec.TOPOS_PROXY, None),
]


def _probe_source_fps(source: Path) -> Rational:
    """探测源真实帧率——时间线帧率必须与源一致，否则平面直通
    的 fps 一致性门会正确拒绝（合成时间线曾硬编码 24fps 造成假慢）。
    """
    try:
        import av

        with av.open(str(source)) as container:
            rate = container.streams.video[0].average_rate
            if rate is not None:
                return Rational(int(rate.numerator), int(rate.denominator))
    except Exception:
        pass
    return Rational(24, 1)


def probe_frames(path: Path, fps: float) -> int:
    """最小帧数探测：优先 Topos 原生，回落 ffprobe/PyAV。"""
    try:
        from src.shared.media.topos_source import ToposMediaSource

        if path.suffix.lower() == ".mov":
            src = ToposMediaSource(str(path))
            src.open()
            try:
                return int(src.frame_count) if hasattr(src, "frame_count") else 10 ** 9
            finally:
                src.close()
    except Exception:
        pass
    try:
        import av

        with av.open(str(path)) as c:
            return int(c.streams.video[0].frames or 10 ** 9)
    except Exception:
        return 10 ** 9


def _probe_source_geometry(source: Path) -> tuple[int, int]:
    """探测源分辨率——时间线分辨率应与源一致（恒等交付是主口径，
    尺寸不符会落入 resize 通道或直通门拒绝）。"""
    try:
        import av

        with av.open(str(source)) as container:
            stream = container.streams.video[0]
            ctx = getattr(stream, "codec_context", None)
            if ctx is not None and ctx.width and ctx.height:
                return int(ctx.width), int(ctx.height)
    except Exception:
        pass
    return 1920, 1080


def build_case(source: Path, frames: int) -> tuple[EditProject, TimelineSequence, dict]:
    fps = _probe_source_fps(source)
    width, height = _probe_source_geometry(source)
    total = min(frames, probe_frames(source, fps))
    if total <= 0:
        raise RuntimeError(f"no frames probe-able in {source.name}")

    project = EditProject.create(name="export-baseline")
    # 用产品导入路径同款探测（含 codec/pixel_format/color 持久化），
    # 合成 metadata 缺 codec 键会让 Topos 直通门 fail-closed 拒绝（假慢）。
    from src.features.edit.core.project import probe_media_metadata
    probed = probe_media_metadata(str(source)) or {}
    metadata = {"fps": float(fps), "total_frames": total}
    metadata.update(probed)
    metadata["total_frames"] = total
    asset = MediaAsset(
        asset_id="asset_baseline",
        path=str(source),
        media_type="video",
        display_name="baseline",
        metadata=metadata,
    )
    project = project.with_media_asset_added(asset)

    clip = ClipInstance(
        clip_id="clip_baseline",
        asset_id=asset.asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=total,
        source_start=0,
        source_duration=total,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    sequence = TimelineSequence(
        timeline_id="tl-baseline", name="Baseline", fps=fps,
        width=width, height=height,
        video_tracks=(v1,), audio_tracks=(),
    )
    return project, sequence, {"frames": total}


def run_case(name: str, codec: ExportCodec, profile, project,
             sequence, frames: int, out_dir: Path) -> dict:
    output_path = out_dir / f"{name}.mov"
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id=sequence.timeline_id,
        range_type=ExportRangeType.ENTIRE,
        codec=codec,
        codec_profile=profile,
        include_audio=False,
        output_path=str(output_path),
    )
    exporter = create_timeline_exporter(project=project, sequence=sequence)

    started = time.perf_counter()
    outcome = exporter.export_timeline(config)
    wall = time.perf_counter() - started

    entry = {
        "case": name,
        "codec": codec.value,
        "source_frames": frames,
        "success": bool(outcome.success),
        "wall_s": round(wall, 3),
        "export_fps": round(frames / wall, 2) if wall > 0 and outcome.success else None,
        "error": None if outcome.success else str(
            getattr(outcome, "error_code", "") or outcome.cause or "failed"),
    }
    if outcome.success and output_path.exists():
        entry["output_bytes"] = output_path.stat().st_size
    else:
        entry["output_bytes"] = None
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="2K_H264.mp4",
                        help="tests/test_videos 下的源素材文件名")
    parser.add_argument("--frames", type=int, default=48,
                        help="每档位导出帧数（默认 48 ≈ 2s @24fps）")
    parser.add_argument("--out", default="bench_out/export_baseline")
    args = parser.parse_args()

    source = VIDEO_DIR / args.source
    if not source.exists():
        print(f"SKIP: source not available: {source}")
        return 0

    out_dir = TEST_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    project, sequence, info = build_case(source, args.frames)
    print(f"source={source.name} frames={info['frames']}")

    results = []
    for name, codec, profile in CODEC_MATRIX:
        label = f"{name} @ {source.name}"
        try:
            entry = run_case(name, codec, profile, project, sequence,
                             info["frames"], out_dir)
            print(f"  {label}: success={entry['success']} "
                  f"fps={entry['export_fps']} wall={entry['wall_s']}s "
                  f"{entry['error'] or ''}")
        except Exception as exc:  # noqa: BLE001 — 单档失败不拖垮整个基线
            entry = {"case": name, "codec": codec.value,
                     "success": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"  {label}: EXCEPTION {entry['error']}")
        results.append(entry)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": "macOS Intel x86_64",
        "source": source.name,
        "frames": info["frames"],
        "results": results,
    }
    out_file = out_dir / f"export_baseline_{source.stem}.json"
    out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"written: {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
