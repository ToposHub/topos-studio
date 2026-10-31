#!/usr/bin/env python
"""M10-4 聚焦实验：顺序解码双帧流水（帧间并行）吞吐与正确性。

对比：
  - serial      ：单句柄顺序解码（现状产品路径）
  - pipeline W=2/3：ParallelSequentialDecoder 条带化双帧/三帧同飞
正确性门禁：逐帧平面 md5 与 serial 逐位一致。
用法：
  TOPOS_CODEC_LIB=... python tools/bench_m104_pipeline.py [--res 4k] [--frames 48]
"""
from __future__ import annotations

import argparse
import hashlib
import zlib
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import bench_topos_vs_dnxhr_prores as B  # noqa: E402
from src.shared.codec import topos_binding as tb  # noqa: E402

import ctypes  # noqa: E402


def serial_decode(path: str, threads: int, n: int, repeats: int) -> dict:
    """单句柄顺序解码（现状），返回 fps 与逐帧 md5。"""
    codec = tb.ToposCodec()
    codec.set_slice_threads(threads)
    movie = tb.ToposMovieFile(codec, path)
    total = min(int(movie.info().sample_count), n)
    info = tb._CFrameOutput()
    ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
    bufs = []

    def geom():
        return [codec.plane_geometry(info, p) for p in range(int(info.plane_count))]

    def run() -> list[str]:
        checksums = []
        for i in range(total):
            buf, size = movie.packet_into(i)
            if not bufs:
                rc = int(codec._lib.tc_frame_decode(
                    buf, ctypes.c_size_t(size), None, None, ctypes.byref(info)))
                assert rc in (0, 1)
                for p, (pw, ph) in enumerate(geom()):
                    arr = (ctypes.c_uint16 * (pw * ph))()
                    bufs.append(arr)
                    ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))
            rc = int(codec._lib.tc_frame_decode(
                buf, ctypes.c_size_t(size), ptrs, None, ctypes.byref(info)))
            assert rc in (0, 1), f"解码失败 rc={rc} @{i}"
            m = 0
            for p, (pw, ph) in enumerate(geom()):
                m = zlib.crc32(bufs[p], m)  # 计时圈内用轻量校验和（md5 会主导耗时）
            checksums.append(f"{m:08x}")
        return checksums

    run()  # 预热
    fps_list = []
    ref = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        cs = run()
        dt = time.perf_counter() - t0
        fps_list.append(total / dt)
        if ref is None:
            ref = cs
    movie.close()
    return {"fps": round(statistics.median(fps_list), 1),
            "checksums": ref}


def pipeline_decode(path: str, threads: int, n: int, repeats: int, workers: int) -> dict:
    """ParallelSequentialDecoder 流水，返回 fps 与逐帧 md5。"""
    import numpy as np
    fps_list = []
    ref = None
    for _ in range(repeats):
        hashes = {}
        t0 = time.perf_counter()
        with tb.ParallelSequentialDecoder(path, workers=workers, threads=threads) as dec:
            count = 0
            for idx, planes in dec:
                if count >= n:
                    break
                m = 0
                for pl in planes:
                    m = zlib.crc32(pl, m)
                hashes[idx] = f"{m:08x}"
                count += 1
        dt = time.perf_counter() - t0
        fps_list.append(count / dt)
        cs = [hashes[i] for i in range(count)]
        if ref is None:
            ref = cs
    return {"fps": round(statistics.median(fps_list), 1), "checksums": ref}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", choices=list(B.SOURCES), default="4k")
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--threads", type=int, nargs="+", default=[8])
    ap.add_argument("--workers", type=int, nargs="+", default=[2, 3])
    ap.add_argument("--slice-rows", type=int, default=16)
    args = ap.parse_args()

    import psutil
    from fractions import Fraction
    from types import SimpleNamespace

    src, w, h = B.SOURCES[args.res]
    out_dir = PROJECT_ROOT / "bench_out" / "m104_pipeline" / (
        f"{time.strftime('%Y%m%d_%H%M%S')}_{args.res}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1] 解码源帧 {args.frames} 帧 @ {w}x{h}", flush=True)
    frames = B.load_source_frames(PROJECT_ROOT / src, w, h, args.frames)

    print(f"[2] 编码测试片 slice_rows={args.slice_rows} …", flush=True)
    from src.shared.export.topos_encoder import ToposVideoEncoder
    cfg = SimpleNamespace(
        width=w, height=h, fps=Fraction(25, 1), pix_fmt="yuv422p10le",
        profile="hq", crf=None, has_alpha=False)
    clip = out_dir / f"clip_sr{args.slice_rows}.mov"
    enc = ToposVideoEncoder(str(clip), cfg, slice_threads=8,
                            slice_rows=args.slice_rows)
    enc.open()
    bf = [B.BenchFrame(p) for p in frames]
    for f in bf:
        enc.encode_frame(f)
    enc.close()

    result = {"meta": {"res": args.res, "frames": args.frames,
                       "slice_rows": args.slice_rows,
                       "logical_cpus": psutil.cpu_count(logical=True),
                       "cpu": B._cpu_name(), "topos_commit": B._git_commit()},
              "rows": {}}

    for threads in args.threads:
        print(f"[3] serial t{threads} …", flush=True)
        base = serial_decode(str(clip), threads, args.frames, 3)
        row = {"serial": {"fps": base["fps"]}}
        for wk in args.workers:
            print(f"[4] pipeline W={wk} t{threads} …", flush=True)
            pipe = pipeline_decode(str(clip), threads, args.frames, 3, wk)
            same = pipe["checksums"] == base["checksums"]
            row[f"pipeline_w{wk}"] = {
                "fps": pipe["fps"], "bitexact_vs_serial": same}
            assert same, f"W={wk} 校验和不一致！"
        result["rows"][f"t{threads}"] = row
        json.dump(result, open(out_dir / "result.json", "w"), indent=1)

    print(json.dumps(result["rows"], indent=1))
    print(f"结果已写入 {out_dir / 'result.json'}")


if __name__ == "__main__":
    main()
