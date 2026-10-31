#!/usr/bin/env python
"""M10-3 phase1 聚焦实验：slice_rows 32/16/8 对编码扩展、码率、解码吞吐的影响。

只跑 hq_rate（产品主档位）：
  - 编码 fps @ t8/t16（并行任务数提升的编码收益）
  - 文件字节数（码率代价，门：≤1%）
  - 解码 fps @ t8/t16（解码墙门：≥8% 提升）
用法：
  TOPOS_CODEC_LIB=... python tools/bench_m103_slice_rows.py [--res 4k] [--frames 24]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import bench_topos_vs_dnxhr_prores as B  # noqa: E402

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", choices=list(B.SOURCES), default="4k")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--slice-rows", type=int, nargs="+", default=[32, 16, 8])
    args = ap.parse_args()

    import psutil

    src, w, h = B.SOURCES[args.res]
    out_dir = PROJECT_ROOT / "bench_out" / "m103_slice_rows" / (
        f"{time.strftime('%Y%m%d_%H%M%S')}_{args.res}")
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "meta": {"res": args.res, "w": w, "h": h, "frames": args.frames,
                 "slice_rows_list": args.slice_rows,
                 "cpu": B._cpu_name(), "logical_cpus": psutil.cpu_count(logical=True),
                 "topos_commit": B._git_commit()},
        "rows": {},
    }

    print(f"[1] 解码源帧 {args.frames} 帧 @ {w}x{h}", flush=True)
    frames = B.load_source_frames(PROJECT_ROOT / src, w, h, args.frames)

    base_bytes = None
    for sr in args.slice_rows:
        row = {}
        for threads in (8, 16):
            key = f"enc_t{threads}"
            print(f"[2] slice_rows={sr} 编码 {key} …", flush=True)
            path = out_dir / f"topos_sr{sr}_t{threads}.mov"
            row[key] = B.topos_encode(frames, str(path), "hq", None, threads, sr)
        path_main = out_dir / f"topos_sr{sr}_t8.mov"
        fb = path_main.stat().st_size
        row["file_bytes"] = fb
        row["file_mbps"] = round(fb * 8 / (args.frames / B.FPS) / 1e6, 1)
        if base_bytes is None:
            base_bytes = fb
        row["bytes_delta_vs_base"] = round((fb - base_bytes) / base_bytes * 100, 3)
        for threads in (8, 16):
            print(f"[3] slice_rows={sr} 解码 t{threads} …", flush=True)
            row[f"dec_t{threads}"] = B.bench_topos_decode(
                str(path_main), threads, args.frames, 3)
        result["rows"][f"sr{sr}"] = row
        json.dump(result, open(out_dir / "result.json", "w"), indent=1)

    print(json.dumps(result["rows"], indent=1))
    print(f"结果已写入 {out_dir / 'result.json'}")


if __name__ == "__main__":
    main()
