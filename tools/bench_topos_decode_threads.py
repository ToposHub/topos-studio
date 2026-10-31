#!/usr/bin/env python3
"""TOPOS 解码吞吐 × 线程数（纯 CPU 机器口径）。

只测"读包 + 解码"本身：预建 plane 视图，计时环内不做哈希/统计（环内取样
会把数字带偏），位精确复验走 --digest 的独立一轮。滤波是解码端行为，故本
表即产品文件的实际解码速度。

用法::

    .venv/bin/python tools/bench_topos_decode_threads.py \
      --files tests/test_videos/2K_topos_422_Proxy.mov --threads 1 4 16 --reps 5

    # 位精确复验：每个线程数各跑一轮全片 SHA-256，必须完全一致
    .venv/bin/python tools/bench_topos_decode_threads.py --files ... --digest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _open(file: str, mode: str = "full"):
    """mode=full：全尺寸交付（交付/导出路径）；mode=scaled：1/2 缩放交付
    （预览路径，走 REDUCED/HALF 请求，滤波在目标网格上以 unit=4 运行）。"""
    from src.shared.codec.topos_binding import (
        TC_DECODE_MODE_REDUCED, TC_DECODE_SCALE_HALF,
        ToposCodec, ToposDecoder, ToposMovieFile,
    )
    codec = ToposCodec()
    movie = ToposMovieFile(codec, file)
    decoder = ToposDecoder(codec)
    total = int(movie.info().sample_count)
    buf, size_b = movie.packet_into(0)
    req = None
    if mode == "scaled":
        req = ToposDecoder.make_request(TC_DECODE_MODE_REDUCED,
                                        scale=TC_DECODE_SCALE_HALF)
        pinfo = decoder.prepare_request(buf, req, size_b)
    else:
        pinfo = decoder.prepare(buf, size_b)
    planes = []
    for k in range(int(pinfo.plane_count)):
        pw, ph = codec.plane_geometry(pinfo, k)
        planes.append(np.empty((ph, pw), dtype="<u2"))
    views = decoder.make_views(planes)
    return codec, movie, decoder, views, planes, int(pinfo.plane_count), total, req


def run(file: str, threads: int, reps: int, with_digest: bool,
        mode: str = "full"):
    """返回 (中位 fps, 各轮 fps, 全片哈希)。每次调用都重开 movie/decoder。"""
    codec, movie, decoder, views, planes, plane_count, total, req = _open(file, mode)
    codec.set_slice_threads(threads)
    fps: list[float] = []
    digest = None
    try:
        for rep in range(reps + (1 if with_digest else 0)):
            is_digest = with_digest and rep == reps
            h = hashlib.sha256() if is_digest else None
            n = 0
            t0 = time.perf_counter()
            for i in range(total):
                buf, size_b = movie.packet_into(i)
                if req is not None:
                    decoder.decode_views_request(buf, views, req, size_b)
                else:
                    decoder.decode_views(buf, views, plane_count, size_b)
                if h is not None:
                    for arr in planes:
                        h.update(np.ascontiguousarray(arr).tobytes())
                n += 1
            dt = time.perf_counter() - t0
            if is_digest:
                digest = h.hexdigest()[:16]
            else:
                fps.append(n / dt if dt > 0 else float("nan"))
    finally:
        decoder.close()
        movie.close()
    return (round(float(np.median(fps)), 1) if fps else float("nan"),
            [round(x, 1) for x in fps], digest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--threads", nargs="+", type=int, default=[1, 4, 16])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--mode", default="full", choices=("full", "scaled"),
                    help="full=全尺寸交付；scaled=1/2 缩放交付（预览路径）")
    ap.add_argument("--digest", action="store_true",
                    help="额外各跑一轮全片 SHA-256（线程数之间必须一致）")
    ap.add_argument("--out", type=Path,
                    default=REPO / "bench_out" / "decode_threads.json")
    args = ap.parse_args()

    res: dict = {}
    for f in args.files:
        name = Path(f).name
        print(f"\n===== {name} =====")
        rows = []
        for th in args.threads:
            med, allf, dig = run(f, th, args.reps, args.digest, args.mode)
            rows.append({"threads": th, "fps_median": med, "fps_all": allf,
                         "digest": dig})
            print(f"  线程 {th:>2}: {med:>7.1f} fps"
                  + (f"   sha256={dig}" if dig else ""))
        res[name] = rows

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(f"\nreport → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
