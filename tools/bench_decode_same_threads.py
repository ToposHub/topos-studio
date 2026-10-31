#!/usr/bin/env python3
"""解码同线程口径对照（M10-6 取证）。

既有 topos_vs_ref 基准里 Topos 用 t8、FFmpeg 参照用 threads=auto（24 逻辑核
机器上远超 8 线程），fps 差距混入线程预算差。本脚本把口径对齐：

- topos：产品解码路径（ToposMovieFile.packet_into + tc_frame_decode），t8；
- ref：PyAV 同文件解码，thread_count=8 × {SLICE（单帧并行，与 topos 同
  语义）、FRAME（帧间并行吞吐参考）}，另附 auto 对照既有基准口径。

用法：
    python tools/bench_decode_same_threads.py --res 2k [--frames 16] [--repeats 3]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

RES_MAP = {
    "2k": {
        "topos": "bench_out/dbg_enc_probe_1920x1080_rate.mov",
        "refs": [("prores", "tests/test_videos/2K_prores422HQ.mov"),
                 ("dnxhr", "tests/test_videos/2K_DNxHR HQX 12-bit.mov")],
    },
    "4k": {
        "topos": "bench_out/dbg_enc_probe_3840x2160_rate.mov",
        "refs": [("prores", "tests/test_videos/4K_prores422HQ.mov"),
                 ("dnxhr", "tests/test_videos/4K_DNxHR HQX 12-bit.mov")],
    },
}


def bench_topos(path: str, threads: int, n: int, repeats: int) -> dict:
    from src.shared.codec import topos_binding as tb

    codec = tb.ToposCodec()
    codec.set_slice_threads(threads)
    movie = tb.ToposMovieFile(codec, path)
    total = int(movie.info().sample_count)
    n = min(n, total)

    info = tb._CFrameOutput()
    buf0, size0 = movie.packet_into(0)
    rc = int(codec._lib.tc_frame_decode(buf0, size0, None, None,
                                        ctypes.byref(info)))
    if rc not in (0, 1):
        raise RuntimeError(f"探测解码失败 rc={rc}")
    ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
    keep = []
    for p in range(int(info.plane_count)):
        pw, ph = codec.plane_geometry(info, p)
        arr = (ctypes.c_uint16 * (pw * ph))()
        keep.append(arr)
        ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))

    def run_seq() -> None:
        for i in range(n):
            buf, size = movie.packet_into(i)
            rc2 = int(codec._lib.tc_frame_decode(
                buf, size, ptrs, None, ctypes.byref(info)))
            if rc2 not in (0, 1):
                raise RuntimeError(f"解码失败 rc={rc2} @ {i}")

    run_seq()  # 预热
    fps_list = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        run_seq()
        fps_list.append(n / (time.perf_counter() - t0))
    movie.close()
    return {"threads": threads, "frames": n,
            "fps_runs": [round(x, 1) for x in fps_list],
            "fps_median": round(statistics.median(fps_list), 1)}


def bench_av(path: str, n: int, repeats: int, threads: int,
             ttype: str) -> dict:
    import av

    with av.open(path) as cont:
        total = sum(1 for _ in cont.decode(video=0))
    n = min(n, total)
    fps_list = []
    for _ in range(repeats):
        cnt = 0
        with av.open(path) as cont:
            ctx = cont.streams.video[0].codec_context
            ctx.thread_count = threads
            ctx.thread_type = ttype
            t0 = time.perf_counter()
            for cnt, _f in enumerate(cont.decode(video=0)):
                if cnt + 1 >= n:
                    break
            fps_list.append(n / (time.perf_counter() - t0))
    return {"threads": threads, "thread_type": ttype, "frames": n,
            "fps_runs": [round(x, 1) for x in fps_list],
            "fps_median": round(statistics.median(fps_list), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", choices=("2k", "4k"), required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    spec = RES_MAP[args.res]
    result = {}
    topos_path = PROJECT_ROOT / spec["topos"]
    if not topos_path.is_file():
        raise SystemExit(f"缺少 Topos 素材 {topos_path}；先跑 dbg_encode_stage_probe.py")
    result["topos_t8"] = bench_topos(str(topos_path), args.threads,
                                     args.frames, args.repeats)
    for name, ref in spec["refs"]:
        rp = PROJECT_ROOT / ref
        if not rp.is_file():
            print(f"[warn] 缺参照文件，跳过: {rp}")
            continue
        result[f"{name}_t8_slice"] = bench_av(str(rp), args.frames,
                                              args.repeats, args.threads, "SLICE")
        result[f"{name}_t8_frame"] = bench_av(str(rp), args.frames,
                                              args.repeats, args.threads, "FRAME")
        result[f"{name}_auto"] = bench_av(str(rp), args.frames,
                                          args.repeats, 0, "FRAME")

    out = Path("bench_out") / "m10_encode_probe"
    out.mkdir(exist_ok=True)
    out_file = out / f"decode_same_threads_{args.res}.json"
    meta = {"res": args.res, "threads": args.threads,
            "logical_cpus": __import__("os").cpu_count()}
    print(json.dumps({"meta": meta, **result}, ensure_ascii=False, indent=2))
    out_file.write_text(json.dumps({"meta": meta, **result},
                                   ensure_ascii=False, indent=2))
    print(f"→ {out_file}")


if __name__ == "__main__":
    main()
