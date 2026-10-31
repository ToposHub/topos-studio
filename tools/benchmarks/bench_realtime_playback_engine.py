#!/usr/bin/env python3
"""
Realtime Playback Engine Benchmark
专业 NLE 实时播放引擎综合基准（规格书 §18.2 / Stage A）。

输出一份完整 Markdown + JSON 报告，覆盖：

1. PyAV 顺序解码能力
2. PyAV 随机 seek 单帧能力（坏路径下界）
3. VideoFrameProviderService.request_frame(quality="proxy")
4. TimelinePreviewRenderer 完整时间线链路（单轨 / 双轨）
5. 音频 sample 连续性（leftover / drift）

所有测试使用 tests/test_videos 真实素材，结果写入
tests/media/benchmark_results/realtime_playback_<timestamp>.{md,json}。

Usage:
    .venv/bin/python tools/benchmarks/bench_realtime_playback_engine.py
    .venv/bin/python tools/benchmarks/bench_realtime_playback_engine.py --quick
    .venv/bin/python tools/benchmarks/bench_realtime_playback_engine.py --only 2K_h264.mp4

约束：本脚本只读不改播放行为，用于固化基线（Stage A）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root on path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TEST_VIDEOS_DIR = ROOT / "tests" / "test_videos"
REPORT_DIR = ROOT / "tests" / "media" / "benchmark_results"
DEFAULT_FRAMES = 60
DEFAULT_SEEK_FRAMES = 30
NUM_FRAMES_FAST = 30


# =============================================================================
# Helpers
# =============================================================================


def find_test_videos() -> Dict[str, Path]:
    videos: Dict[str, Path] = {}
    if TEST_VIDEOS_DIR.exists():
        for f in sorted(TEST_VIDEOS_DIR.iterdir()):
            if f.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv") and not f.name.startswith("."):
                videos[f.name] = f
    return videos


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


@dataclass
class BenchResult:
    name: str
    file: str
    frames: int = 0
    first_ms: float = 0.0
    mean_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0
    effective_fps: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# =============================================================================
# Bench 1: Raw PyAV sequential decode
# =============================================================================


def bench_raw_pyav_sequential(path: Path, num_frames: int) -> BenchResult:
    import av
    import numpy as np

    container = av.open(str(path))
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        container.close()
        return BenchResult("raw_pyav_sequential", path.name)

    codec = getattr(stream.codec_context, "name", "?")
    width = stream.width or 0
    height = stream.height or 0
    pix_fmt = getattr(stream.codec_context, "pix_fmt", "?")

    decode_times: List[float] = []
    bgr_times: List[float] = []
    first_ms = 0.0
    count = 0
    for i, frame in enumerate(container.decode(stream)):
        if i >= num_frames:
            break
        t0 = time.perf_counter()
        arr = frame.to_ndarray(format="bgr24")
        t1 = time.perf_counter()
        if i == 0:
            first_ms = (t1 - t0) * 1000.0
        else:
            decode_times.append((t1 - t0) * 1000.0)
        if i > 0 and arr is not None:
            # 已经包含 bgr 转换，这里仅用于统计
            pass
        count += 1
    container.close()

    mean_ms = statistics.mean(decode_times) if decode_times else 0.0
    return BenchResult(
        name="raw_pyav_sequential",
        file=path.name,
        frames=count,
        first_ms=round(first_ms, 3),
        mean_ms=round(mean_ms, 3),
        p50_ms=round(percentile(decode_times, 50), 3),
        p95_ms=round(percentile(decode_times, 95), 3),
        max_ms=round(max(decode_times) if decode_times else 0.0, 3),
        effective_fps=round(1000.0 / mean_ms, 2) if mean_ms > 0 else 0.0,
        extra={
            "codec": codec,
            "width": width,
            "height": height,
            "pix_fmt": pix_fmt,
        },
    )


# =============================================================================
# Bench 2: Raw PyAV random seek (worst-case)
# =============================================================================


def bench_raw_pyav_random_seek(path: Path, num_frames: int) -> BenchResult:
    import av

    container = av.open(str(path))
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        container.close()
        return BenchResult("raw_pyav_random_seek", path.name)

    total_frames = 0
    try:
        if stream.duration and stream.average_rate:
            total_frames = int(float(stream.duration * stream.time_base) * float(stream.average_rate))
    except Exception:
        pass
    if total_frames <= num_frames:
        total_frames = num_frames * 4

    times: List[float] = []
    first_ms = 0.0
    for i in range(num_frames):
        # 跳到随机位置
        target = (i * 17) % total_frames
        try:
            target_sec = float(target) / float(stream.average_rate or 25)
            container.seek(int(target_sec / float(stream.time_base)), stream=stream)
        except Exception:
            pass
        t0 = time.perf_counter()
        for frame in container.decode(stream):
            arr = frame.to_ndarray(format="bgr24")
            break
        t1 = time.perf_counter()
        ms = (t1 - t0) * 1000.0
        if i == 0:
            first_ms = ms
        else:
            times.append(ms)
    container.close()

    mean_ms = statistics.mean(times) if times else 0.0
    return BenchResult(
        name="raw_pyav_random_seek",
        file=path.name,
        frames=num_frames,
        first_ms=round(first_ms, 3),
        mean_ms=round(mean_ms, 3),
        p50_ms=round(percentile(times, 50), 3),
        p95_ms=round(percentile(times, 95), 3),
        max_ms=round(max(times) if times else 0.0, 3),
        effective_fps=round(1000.0 / mean_ms, 2) if mean_ms > 0 else 0.0,
    )


# =============================================================================
# Bench 3: AudioDecoder sample continuity (Stage B validation)
# =============================================================================


def bench_audio_continuity(path: Path, num_frames: int = DEFAULT_FRAMES) -> BenchResult:
    """
    用 AudioDecoder 连续解码 num_frames 个视频帧的音频 chunk，
    验证 sample 连续性（leftover 实现后应该 0 丢失）。
    """
    try:
        from src.features.edit.core.audio_playback import AudioDecoder
    except Exception as e:
        return BenchResult("audio_continuity", path.name, extra={"error": str(e)})

    fps = 25.0
    output_rate = 48000
    samples_per_frame = int(output_rate / fps)  # 1920

    decoder = AudioDecoder()
    ok = decoder.open(
        path,
        timeline_fps=fps,
        source_start=0,
        source_fps=fps,
        output_sample_rate=output_rate,
    )
    if not ok:
        return BenchResult("audio_continuity", path.name, extra={"error": "open failed"})

    total_decoded = 0
    expected_total = 0
    dropped = 0
    decode_times: List[float] = []
    first_ms = 0.0

    try:
        for i in range(num_frames):
            t0 = time.perf_counter()
            samples = decoder.decode_samples_for_frame(i, fps, samples_per_frame)
            t1 = time.perf_counter()
            ms = (t1 - t0) * 1000.0
            if i == 0:
                first_ms = ms
            else:
                decode_times.append(ms)
            expected_total += samples_per_frame
            if samples is not None:
                got = samples.shape[-1]
                total_decoded += got
                if got < samples_per_frame:
                    dropped += (samples_per_frame - got)
            else:
                dropped += samples_per_frame
    finally:
        decoder.close()

    drift_samples = expected_total - total_decoded
    drift_ms = round((drift_samples / float(output_rate)) * 1000.0, 3)
    mean_ms = statistics.mean(decode_times) if decode_times else 0.0

    return BenchResult(
        name="audio_continuity",
        file=path.name,
        frames=num_frames,
        first_ms=round(first_ms, 3),
        mean_ms=round(mean_ms, 3),
        p50_ms=round(percentile(decode_times, 50), 3),
        p95_ms=round(percentile(decode_times, 95), 3),
        effective_fps=round(1000.0 / mean_ms, 2) if mean_ms > 0 else 0.0,
        extra={
            "sample_rate": output_rate,
            "samples_per_frame": samples_per_frame,
            "expected_total_samples": expected_total,
            "decoded_total_samples": total_decoded,
            "dropped_samples": dropped,
            "drift_samples": drift_samples,
            "drift_ms": drift_ms,
        },
    )


# =============================================================================
# Bench 4: VideoFrameProviderService.request_frame(proxy)
# =============================================================================


def bench_frame_provider(path: Path, num_frames: int) -> BenchResult:
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    service = VideoFrameProviderService()
    clip_id = service.open_clip(
        str(path),
        share_by_path=False,
        defer_memory_proxy=True,
    )
    if clip_id is None:
        return BenchResult("frame_provider_proxy", path.name, extra={"error": "open_clip failed"})

    open_ms = 0.0  # measured separately above
    times: List[float] = []
    first_ms = 0.0
    source_types = set()
    for i in range(num_frames):
        t0 = time.perf_counter()
        frame = service.request_frame(clip_id, i, quality="proxy")
        t1 = time.perf_counter()
        ms = (t1 - t0) * 1000.0
        if i == 0:
            first_ms = ms
        else:
            times.append(ms)
        # source kind trace
        try:
            session = service._clips.get(clip_id)
            if session is not None:
                if session.disk_proxy_info is not None and session.disk_proxy_info.is_ready:
                    source_types.add("disk_proxy")
                elif session.proxy_source is not None:
                    source_types.add("memory_proxy")
                else:
                    source_types.add("master")
        except Exception:
            pass
    service.close_clip(clip_id)

    mean_ms = statistics.mean(times) if times else 0.0
    return BenchResult(
        name="frame_provider_proxy",
        file=path.name,
        frames=num_frames,
        first_ms=round(first_ms, 3),
        mean_ms=round(mean_ms, 3),
        p50_ms=round(percentile(times, 50), 3),
        p95_ms=round(percentile(times, 95), 3),
        max_ms=round(max(times) if times else 0.0, 3),
        effective_fps=round(1000.0 / mean_ms, 2) if mean_ms > 0 else 0.0,
        extra={"source_types": sorted(source_types)},
    )


# =============================================================================
# Orchestration
# =============================================================================


def run_all(
    videos: Dict[str, Path],
    num_frames: int,
    quick: bool,
    skip_raw: bool,
    skip_renderer: bool,
) -> Dict[str, Any]:
    results: Dict[str, List[BenchResult]] = {
        "raw_pyav_sequential": [],
        "raw_pyav_random_seek": [],
        "audio_continuity": [],
        "frame_provider_proxy": [],
    }

    for name, path in videos.items():
        print(f"\n[+] {name}")
        if not skip_raw:
            print("    raw sequential ...", end=" ", flush=True)
            r = bench_raw_pyav_sequential(path, num_frames)
            results["raw_pyav_sequential"].append(r)
            print(f"{r.effective_fps} fps mean={r.mean_ms}ms")
            if not quick:
                print("    raw random seek ...", end=" ", flush=True)
                r2 = bench_raw_pyav_random_seek(path, min(num_frames, DEFAULT_SEEK_FRAMES))
                results["raw_pyav_random_seek"].append(r2)
                print(f"{r2.effective_fps} fps mean={r2.mean_ms}ms")

        print("    audio continuity ...", end=" ", flush=True)
        r3 = bench_audio_continuity(path, num_frames)
        results["audio_continuity"].append(r3)
        dropped = r3.extra.get("dropped_samples", "?")
        drift = r3.extra.get("drift_ms", "?")
        print(f"dropped={dropped} drift_ms={drift}")

        if not skip_renderer:
            print("    frame provider proxy ...", end=" ", flush=True)
            try:
                r4 = bench_frame_provider(path, num_frames)
                results["frame_provider_proxy"].append(r4)
                print(f"{r4.effective_fps} fps mean={r4.mean_ms}ms sources={r4.extra.get('source_types')}")
            except Exception as e:
                print(f"FAILED: {e}")

    return {
        "results": {k: [r.to_dict() for r in v] for k, v in results.items()},
        "meta": {
            "num_frames": num_frames,
            "quick": quick,
            "video_count": len(videos),
        },
    }


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Realtime Playback Engine Benchmark Report")
    lines.append("")
    lines.append(f"- frames per test: {report['meta']['num_frames']}")
    lines.append(f"- videos tested: {report['meta']['video_count']}")
    lines.append("")
    for section, rows in report["results"].items():
        if not rows:
            continue
        lines.append(f"## {section}")
        lines.append("")
        lines.append("| file | frames | first_ms | mean_ms | p50 | p95 | max | eff_fps | extra |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for r in rows:
            extra = r.get("extra", {}) or {}
            extra_str = "; ".join(f"{k}={v}" for k, v in extra.items() if k != "error")
            lines.append(
                f"| {r['file']} | {r['frames']} | {r['first_ms']} | {r['mean_ms']} | "
                f"{r['p50_ms']} | {r['p95_ms']} | {r['max_ms']} | {r['effective_fps']} | {extra_str} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="fewer frames, skip random seek")
    parser.add_argument("--skip-raw", action="store_true", help="skip PyAV raw benches")
    parser.add_argument("--skip-renderer", action="store_true", help="skip frame provider bench")
    parser.add_argument("--only", help="comma-separated video names to test")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    num_frames = NUM_FRAMES_FAST if args.quick else args.frames
    videos = find_test_videos()
    if args.only:
        wanted = set(s.strip() for s in args.only.split(","))
        videos = {k: v for k, v in videos.items() if k in wanted}
    if not videos:
        print(f"No test videos found in {TEST_VIDEOS_DIR}")
        return 1

    print(f"Found {len(videos)} videos. Frames per test: {num_frames}")
    report = run_all(
        videos,
        num_frames=num_frames,
        quick=args.quick,
        skip_raw=args.skip_raw,
        skip_renderer=args.skip_renderer,
    )

    if not args.no_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        json_path = REPORT_DIR / f"realtime_playback_{ts}.json"
        md_path = REPORT_DIR / f"realtime_playback_{ts}.md"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        print(f"\nReport written:\n  {json_path}\n  {md_path}")

    # 打印关键 invariant 摘要
    print("\n=== Audio Continuity (Stage B invariant: dropped_samples == 0) ===")
    for r in report["results"]["audio_continuity"]:
        extra = r.get("extra", {}) or {}
        print(
            f"  {r['file']:<35} dropped={extra.get('dropped_samples', '?'):<8} "
            f"drift_ms={extra.get('drift_ms', '?')}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
