#!/usr/bin/env python
"""Sequential-decode throughput comparison for one asset: VT vs FFmpeg.

Measures pure decode latency for forward playback (the pattern the playback
path needs), for sizing codec-routing decisions (e.g. 10-bit HEVC where VT
falls back to software).

Run:
  .venv/bin/python tools/bench_playback_decode_compare.py 4K_H265
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

VIDEOS = {
    "2K_h264": "tests/test_videos/2K_h264.mp4",
    "2K_H265": "tests/test_videos/2K_H265.mov",
    "2K_prores422HQ": "tests/test_videos/2K_prores422HQ.mov",
    "2K_DNxHR": "tests/test_videos/2K_DNxHR HQX 12-bit.mov",
    "4K_H264": "tests/test_videos/4K_H264.mov",
    "4K_H265": "tests/test_videos/4K_H265.mov",
    "4K_prores422HQ": "tests/test_videos/4K_prores422HQ.mov",
    "4K_prores4444": "tests/test_videos/4K_prores4444.mov",
    "4K_DNxHR": "tests/test_videos/4K_DNxHR HQX 12-bit.mov",
}


def bench_ffmpeg(path: str, n: int = 120) -> list[float]:
    from src.shared.media.ffmpeg_source import FFmpegMediaSource
    src = FFmpegMediaSource(path)
    src.open()
    times = []
    t_all = time.perf_counter()
    for i in range(n):
        t0 = time.perf_counter()
        f = src.read_frame_planar(i) if hasattr(src, "read_frame_planar") else src.read_frame(i)
        times.append((time.perf_counter() - t0) * 1000.0)
        if f is None:
            break
    total = time.perf_counter() - t_all
    src.close()
    return times, total


def bench_vt(path: str, n: int = 120):
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource
    src = MacOSVtZeroCopySource()
    src.open(path)
    times = []
    t_all = time.perf_counter()
    for i in range(n):
        t0 = time.perf_counter()
        f = src.read_frame_pixel_buffer(i)
        times.append((time.perf_counter() - t0) * 1000.0)
        if f is None:
            break
    total = time.perf_counter() - t_all
    src.close()
    return times, total


def stats(times: list[float]):
    if not times:
        return 0.0, 0.0
    # 先按时间序丢预热帧再排序；顺序颠倒会丢掉最快样本而非慢的预热帧。
    steady_times = times[10:] if len(times) > 20 else times
    s = sorted(steady_times)
    return s[len(s) // 2], s[-1] if s else 0.0


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "4K_H265"
    path = os.path.join(ROOT, VIDEOS[name])
    print(f"== sequential decode: {name} ==")

    for label, fn in (("ffmpeg", bench_ffmpeg), ("vt", bench_vt)):
        try:
            times, total = fn(path)
            p50, worst = stats(times)
            fps = len(times) / total if total else 0.0
            print(f"  {label:8s} n={len(times):3d} p50={p50:6.1f}ms worst(steady)={worst:7.1f}ms "
                  f"throughput={fps:6.1f}fps")
        except Exception as e:
            print(f"  {label:8s} FAILED: {e}")


if __name__ == "__main__":
    main()
