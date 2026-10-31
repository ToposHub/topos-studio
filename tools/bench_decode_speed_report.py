#!/usr/bin/env python
"""Decode-speed benchmark across codecs and resolutions, with report output.

Benchmarks the project's two production decode paths on every asset in
tests/test_videos:

  - FFmpeg software decode (FFmpegMediaSource.read_frame_planar, the
    planar-YUV path the color pipeline consumes)
  - macOS VideoToolbox zero-copy decode (MacOSVtZeroCopySource on Intel;
    falls back per codec availability)

Outputs:
  docs/decode_speed_benchmark_<date>.json  raw per-frame stats
  stdout summary table

Run:
  .venv/bin/python tools/bench_decode_speed_report.py [--n 120]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ASSETS = {
    "2K_H264":         "tests/test_videos/2K_h264.mp4",
    "2K_H265":         "tests/test_videos/2K_H265.mov",
    "2K_ProRes422HQ":  "tests/test_videos/2K_prores422HQ.mov",
    "2K_DNxHR_HQX":    "tests/test_videos/2K_DNxHR HQX 12-bit.mov",
    "4K_H264":         "tests/test_videos/4K_H264.mov",
    "4K_H265":         "tests/test_videos/4K_H265.mov",
    "4K_ProRes422HQ":  "tests/test_videos/4K_prores422HQ.mov",
    "4K_ProRes4444":   "tests/test_videos/4K_prores4444.mov",
    "4K_DNxHR_HQX":    "tests/test_videos/4K_DNxHR HQX 12-bit.mov",
}


def probe(path: str) -> dict:
    import av

    size = os.path.getsize(path)
    with av.open(path) as c:
        s = c.streams.video[0]
        cc = s.codec_context
        duration = None
        if s.duration and s.time_base:
            duration = float(s.duration * s.time_base)
        if not duration and cc.duration and cc.time_base:
            duration = float(cc.duration * cc.time_base)
        if not duration and s.average_rate and s.frames:
            duration = float(s.frames / s.average_rate)
        pix = cc.pix_fmt or ""
        bitdepth = 8
        for marker, depth in (("12", 12), ("16", 16), ("10", 10)):
            if marker in pix:
                bitdepth = depth
                break
        return {
            "codec": cc.name,
            "profile": cc.profile or "",
            "pix_fmt": pix,
            "bit_depth": bitdepth,
            "width": cc.width,
            "height": cc.height,
            "fps": float(s.average_rate) if s.average_rate else 0.0,
            "frames": int(s.frames or 0),
            "duration_s": round(duration, 3) if duration else None,
            "size_mb": round(size / (1024 * 1024), 1),
            "bitrate_mbps": round(size * 8 / duration / 1e6, 1) if duration else None,
        }


def stats(times: list[float]) -> dict:
    # 先按时间序丢弃预热帧，再排序求分位；顺序颠倒会把最快样本当预热丢掉。
    steady_times = times[10:] if len(times) > 20 else times
    s = sorted(steady_times)
    return {
        "n": len(s),
        "p50_ms": round(statistics.median(s), 2),
        "p95_ms": round(s[int(0.95 * (len(s) - 1))], 2),
        "worst_steady_ms": round(s[-1], 2) if s else None,
    }


def bench_ffmpeg(path: str, n: int) -> dict:
    from src.shared.media.ffmpeg_source import FFmpegMediaSource

    src = FFmpegMediaSource(path)
    t0 = time.perf_counter()
    src.open()
    open_ms = (time.perf_counter() - t0) * 1000.0
    times = []
    t_all = time.perf_counter()
    try:
        for i in range(n):
            t = time.perf_counter()
            f = (
                src.read_frame_planar(i)
                if hasattr(src, "read_frame_planar")
                else src.read_frame(i)
            )
            times.append((time.perf_counter() - t) * 1000.0)
            if f is None:
                break
    finally:
        total = time.perf_counter() - t_all
        src.close()
    out = stats(times)
    out.update(open_ms=round(open_ms, 1), total_s=round(total, 2),
               throughput_fps=round(len(times) / total, 1) if total else 0.0)
    return out


def bench_vt(path: str, n: int) -> dict:
    from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

    src = MacOSVtZeroCopySource()
    t0 = time.perf_counter()
    ok = src.open(path)
    open_ms = (time.perf_counter() - t0) * 1000.0
    if not ok:
        return {"ok": False, "error": "open() returned False (codec unsupported)",
                "open_ms": round(open_ms, 1)}
    times = []
    t_all = time.perf_counter()
    try:
        for i in range(n):
            t = time.perf_counter()
            f = src.read_frame_pixel_buffer(i)
            times.append((time.perf_counter() - t) * 1000.0)
            if f is None:
                break
    except Exception as e:  # mid-stream session failure counts as data
        total = time.perf_counter() - t_all
        out = stats(times) if times else {"n": 0, "p50_ms": None, "p95_ms": None,
                                          "worst_steady_ms": None}
        out.update(open_ms=round(open_ms, 1), total_s=round(total, 2),
                   throughput_fps=round(len(times) / total, 1) if total else 0.0,
                   error=f"{type(e).__name__}: {e}")
        return out
    finally:
        src.close()
    total = time.perf_counter() - t_all
    out = stats(times)
    out.update(open_ms=round(open_ms, 1), total_s=round(total, 2),
               throughput_fps=round(len(times) / total, 1) if total else 0.0)
    return out


def machine_info() -> dict:
    info = {
        "platform": platform.platform(),
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
        "machine": platform.machine(),
    }
    try:
        info["cpu"] = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        info["cores"] = subprocess.run(
            ["sysctl", "-n", "hw.ncpu"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        pass
    try:
        import av
        info["pyav"] = av.__version__
    except Exception:
        pass
    return info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120, help="frames per asset")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    results = {"date": str(date.today()), "machine": machine_info(),
               "frames_per_asset": args.n, "assets": []}

    for name, rel in ASSETS.items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"{name}: MISSING {rel}", flush=True)
            continue
        meta = probe(path)
        print(f"== {name}  {meta['width']}x{meta['height']} {meta['codec']}/"
              f"{meta['pix_fmt']} {meta['fps']:.0f}fps ==", flush=True)

        try:
            ff = bench_ffmpeg(path, args.n)
        except Exception as e:
            ff = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"   ffmpeg sw : {ff}", flush=True)

        try:
            vt = bench_vt(path, args.n)
        except Exception as e:
            vt = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"   vt hw     : {vt}", flush=True)

        results["assets"].append({"name": name, "file": rel, "probe": meta,
                                  "ffmpeg": ff, "vt": vt})

    json_path = args.json_out or os.path.join(
        ROOT, "docs", f"decode_speed_benchmark_{date.today():%Y-%m-%d}.json")
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nJSON written: {json_path}", flush=True)


if __name__ == "__main__":
    main()
