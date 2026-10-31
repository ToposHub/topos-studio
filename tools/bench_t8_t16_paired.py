#!/usr/bin/env python3
"""t8 vs t16 交错成对内核解码基准（阶段4 纯内核批量口径）。

背景：M10-6 上限 8→16 后 thread_default = min(ncpu, TC_SLICE_MAX_THREADS)
在 16 逻辑核机上默认即为 16。本脚本以成对差分（序平衡交错进程对）
复核默认工作点：每对两个独立子进程（env TOPOS_SLICE_THREADS=8/16），
子进程预取全部包 → 1 遍预热 + N 遍计时（tc_decoder_decode_batch 批解，
bs 随分辨率），报每遍 fps 与中位。父进程逐对记录 uptime 采样。

重要：make_views 仅存平面数组地址——子进程必须持有数组引用
（keep 列表），否则平面被释放、worker 写入已释放缓冲（远端段错误，
2026-09-02 静机复验首版脚本即踩此坑）。

用法：
  tools/bench_t8_t16_paired.py [--pairs 12] [--passes 5]
      [--movie-2k PATH] [--movie-4k PATH] [--arms 8 16]
默认码流为阶段4 产物（缺失时报错退出；生成方式见
bench_out/m11_stage4/20260902_stage4_decode_batch.txt）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, List

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_2K = os.path.join(_REPO, "bench_out/topos_vs_ref/20260902_065735_2k/topos_hq_rate_t8.mov")
_DEFAULT_4K = os.path.join(_REPO, "bench_out/topos_vs_ref/20260902_065819_4k/topos_hq_rate_t8.mov")

CHILD = r'''
import json, os, statistics, sys, time
sys.path.insert(0, os.environ["REPO"])
import numpy as np
import src.shared.codec.topos_binding as tb
path, bs, passes = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
codec = tb.ToposCodec()
mv = tb.ToposMovieFile(codec, path)
dec = tb.ToposDecoder(codec)
total = int(mv.info().sample_count)
pkts = [mv.packet(i) for i in range(total)]
p0 = dec.prepare(pkts[0])
pc = int(p0.plane_count)
geoms = [codec.plane_geometry(p0, p) for p in range(pc)]
frames = []
keep = []  # 平面数组保活：make_views 仅存地址，不持引用
for i in range(total):
    arrs = [np.zeros((h, w), dtype="<u2") for (w, h) in geoms]
    keep.extend(arrs)
    frames.append(dec.make_views(arrs))
def one_pass():
    t0 = time.perf_counter()
    for s in range(0, total, bs):
        dec.decode_views_batch(pkts[s:s+bs], frames[s:s+bs])
    return total / (time.perf_counter() - t0)
one_pass()
fps = [one_pass() for _ in range(passes)]
dec.close(); mv.close()
print(json.dumps({"threads": int(os.environ["TOPOS_SLICE_THREADS"]),
                  "total": total, "fps": fps, "median": statistics.median(fps)}))
'''


def _sample_load() -> str:
    with subprocess.Popen(["uptime"], stdout=subprocess.PIPE) as p:
        out, _ = p.communicate()
    return out.decode().strip()


def _child(py: str, repo: str, path: str, bs: int, threads: int, passes: int) -> Dict[str, Any]:
    env = dict(os.environ, REPO=repo, TOPOS_SLICE_THREADS=str(threads))
    r = subprocess.run([py, "-c", CHILD, path, str(bs), str(passes)],
                       capture_output=True, text=True, env=env, cwd=repo)
    if r.returncode != 0:
        sys.stderr.write(f"child rc={r.returncode} t={threads}\n{r.stderr[-2000:]}\n")
        raise SystemExit(f"child failed t={threads} {path}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", type=int, default=12)
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--movie-2k", default=_DEFAULT_2K)
    ap.add_argument("--movie-4k", default=_DEFAULT_4K)
    ap.add_argument("--arms", type=int, nargs=2, default=[8, 16], metavar=("LO", "HI"))
    args = ap.parse_args()

    py = os.path.join(_REPO, ".venv/bin/python3")
    if not os.path.exists(py):
        py = sys.executable
    lo, hi = args.arms
    res = [("2k", args.movie_2k, 8), ("4k", args.movie_4k, 4)]
    for _, path, _ in res:
        if not os.path.exists(path):
            raise SystemExit(f"码流不存在：{path}（生成方式见 m11_stage4 证据）")

    rows: List[Dict[str, Any]] = []
    for name, path, bs in res:
        for k in range(args.pairs):
            load_before = _sample_load()
            order = (lo, hi) if k % 2 == 0 else (hi, lo)
            row: Dict[str, Any] = {"res": name, "pair": k, "order": order,
                                   "load": load_before + " || after: " + _sample_load()}
            for t in order:
                row[str(t)] = _child(py, _REPO, path, bs, t, args.passes)
            row["ratio_hi_over_lo"] = row[str(hi)]["median"] / row[str(lo)]["median"]
            rows.append(row)
            print(f"[{name}] pair {k:2d} order={order} t{lo}={row[str(lo)]['median']:7.1f} "
                  f"t{hi}={row[str(hi)]['median']:7.1f} ratio={row['ratio_hi_over_lo']:.3f}",
                  flush=True)

    print()
    for name, _, _ in res:
        rs = [r["ratio_hi_over_lo"] for r in rows if r["res"] == name]
        mlo = statistics.median(r[str(lo)]["median"] for r in rows if r["res"] == name)
        mhi = statistics.median(r[str(hi)]["median"] for r in rows if r["res"] == name)
        print(f"== {name}: median t{lo}={mlo:.1f} t{hi}={mhi:.1f} "
              f"ratio_median={statistics.median(rs):.3f} all={[f'{x:.3f}' for x in rs]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
