#!/usr/bin/env python3
"""Topos 解码性能基准（M0 观测先行，计划 §4.1）。

口径：
  wrapper  —— 产品路径：ToposMovieFile.packet(index) + ToposCodec.decode(packet)
              （含 packet 探测/复制、query+full 两次 ctypes 调用、bytes 输出）
  native   —— 预取 packet 进内存 + 预分配输出平面 + 单次 tc_frame_decode
              （M2 目标口径的近似，度量 native 内核进度）

用法：
    .venv/bin/python tools/bench_topos_decode.py [--frames 120] [--threads 4]
        [--videos tests/test_videos/2K_topos.mov tests/test_videos/4K_topos.mov]
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def bench_wrapper(path: str, frames: int) -> dict:
    from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile

    codec = ToposCodec()
    movie = ToposMovieFile(codec, path)
    total = movie.info().sample_count
    n = min(frames, total)
    times = []
    for i in range(12):  # 预热
        movie.packet(i % total)
    for i in range(12, 12 + n):
        pkt = None
        t0 = time.perf_counter()
        pkt = movie.packet(i % total)
        frame = codec.decode(pkt)
        times.append((time.perf_counter() - t0) * 1000.0)
        del frame, pkt
    movie.close()
    return _stats(times)


def bench_native(lib_path: str | None, path: str, frames: int) -> dict:
    """预取 packet + 预分配输出，只调一次 tc_frame_decode（M2 目标口径近似）。"""
    import ctypes
    from src.shared.codec import topos_binding as tb

    codec = tb.ToposCodec()
    movie = tb.ToposMovieFile(codec, path)
    total = movie.info().sample_count
    n = min(frames, total)

    # 预取全部 packet（消除 IO 影响后测纯解码内核）
    packets = [movie.packet(i % total) for i in range(12 + n)]

    # 预分配输出平面（uint16 ctypes 数组，解码直写）
    info = tb._CFrameOutput()
    buf0 = ctypes.create_string_buffer(packets[0], max(len(packets[0]), 1))
    rc = codec._lib.tc_frame_decode(buf0, len(packets[0]), None, None, ctypes.byref(info))
    assert rc in (0, 1), f"query rc={rc}"
    planes = []
    ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
    for p in range(info.plane_count):
        w, h = codec.plane_geometry(info, p)
        arr = (ctypes.c_uint16 * (w * h))()
        planes.append(arr)
        ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))
    bufs = [ctypes.create_string_buffer(pkt, max(len(pkt), 1)) for pkt in packets]

    times = []
    for i in range(12):  # 预热
        codec._lib.tc_frame_decode(bufs[i], len(packets[i]), ptrs, None, ctypes.byref(info))
    for i in range(12, 12 + n):
        t0 = time.perf_counter()
        rc = codec._lib.tc_frame_decode(bufs[i], len(packets[i]), ptrs, None,
                                        ctypes.byref(info))
        times.append((time.perf_counter() - t0) * 1000.0)
        assert rc in (0, 1), f"decode rc={rc}"
    movie.close()
    return _stats(times)


def _stats(times: list[float]) -> dict:
    s = sorted(times)
    return {
        "n": len(s),
        "avg_ms": round(statistics.fmean(s), 2),
        "p50_ms": round(statistics.median(s), 2),
        "p95_ms": round(s[int(0.95 * (len(s) - 1))], 2),
        "max_ms": round(s[-1], 2),
        "fps": round(1000.0 / statistics.fmean(s), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--videos", nargs="+", default=[
        str(PROJECT_ROOT / "tests/test_videos/2K_topos.mov"),
        str(PROJECT_ROOT / "tests/test_videos/4K_topos.mov"),
    ])
    args = ap.parse_args()

    if args.threads is not None:
        os.environ["TOPOS_SLICE_THREADS"] = str(args.threads)

    for path in args.videos:
        if not Path(path).exists():
            print(f"跳过（不存在）: {path}")
            continue
        print(f"== {Path(path).name} ==")
        r = bench_wrapper(path, args.frames)
        print(f"  wrapper : avg={r['avg_ms']:>7.2f}ms p50={r['p50_ms']:>7.2f}ms "
              f"p95={r['p95_ms']:>7.2f}ms fps={r['fps']:>6.1f}")
        r = bench_native(None, path, args.frames)
        print(f"  native  : avg={r['avg_ms']:>7.2f}ms p50={r['p50_ms']:>7.2f}ms "
              f"p95={r['p95_ms']:>7.2f}ms fps={r['fps']:>6.1f}")


if __name__ == "__main__":
    main()
