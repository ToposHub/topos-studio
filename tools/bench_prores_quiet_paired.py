#!/usr/bin/env python3
"""ProRes/DNxHR 解码基线静机同窗成对重测（2026-09-02 候选①）。

背景：记分牌锚点「2K ProRes 同窗 448.2」为脏机窗口口径（M10-6.4 证据，
~07:00），而静机下 Topos 2K 内核批解已达 ~486fps——公平比值需静机重钉。
本脚本把两侧放进同一窗口成对交错：每对按臂序跑 5 个独立子进程，
臂序随对奇偶轮转（序平衡），逐对计算各臂对 ProRes slice 锚点的比值，
中位数消机器慢漂移。

臂（全部 2K=36 / 4K=24 帧每遍，与历史口径一致）：
  - topos_kernel：纯内核批解（预取包 + decode_views_batch，bs 2K=8/4K=4），t8，
    hq rate 码流（2K 193.6 / 4K 767.4 Mbps）—— 429.4（脏）口径
  - topos_kernel_std：同上，standard rate 码流（2K 127.6 / 4K 535.3 Mbps）
    —— 同码率档对照（ProRes HQ 2K 107 / 4K ~430 Mbps）
  - topos_product：产品逐帧路径（packet_into + tc_frame_decode），t8
    —— 342.4 参考口径
  - prores_slice：PyAV thread_count=8 SLICE —— 448.2 锚点口径
  - prores_frame：PyAV thread_count=8 FRAME —— 449.2 口径
  - dnxhr_slice：PyAV thread_count=8 SLICE —— 459.6 口径

子进程隔离（各自线程池/缓存态），先 1 遍预热（页缓存+分支预热）再计时。
平面保活：make_views 仅存地址，keep 列表必须持有 numpy 数组引用。

用法：
  .venv/bin/python tools/bench_prores_quiet_paired.py --res 2k \
      [--pairs 12] [--passes 5]
输出：bench_out/topos_vs_ref/<时间戳>_quiet<res>/result.json + 控制台摘要
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]

SPECS = {
    "2k": {
        "topos": "bench_out/topos_vs_ref/20260902_065735_2k/topos_hq_rate_t8.mov",
        "topos_std": "bench_out/topos_vs_ref/20260902_065735_2k/topos_standard_rate.mov",
        "prores": "tests/test_videos/2K_prores422HQ.mov",
        "dnxhr": "tests/test_videos/2K_DNxHR HQX 12-bit.mov",
        "frames": 36,
        "bs": 8,
    },
    "4k": {
        "topos": "bench_out/topos_vs_ref/20260902_065819_4k/topos_hq_rate_t8.mov",
        "topos_std": "bench_out/topos_vs_ref/20260902_065819_4k/topos_standard_rate.mov",
        "prores": "tests/test_videos/4K_prores422HQ.mov",
        "dnxhr": "tests/test_videos/4K_DNxHR HQX 12-bit.mov",
        "frames": 24,
        "bs": 4,
    },
}

ARMS = ["topos_kernel", "topos_product", "topos_kernel_std",
        "prores_slice", "prores_frame", "dnxhr_slice"]
ANCHOR = "prores_slice"

CHILD = r'''
import json, statistics, sys, time
sys.path.insert(0, sys.argv[1])
arm, path, frames, passes, bs = (sys.argv[2], sys.argv[3], int(sys.argv[4]),
                                 int(sys.argv[5]), int(sys.argv[6]))

if arm.startswith("topos"):
    import ctypes
    import src.shared.codec.topos_binding as tb
    codec = tb.ToposCodec()
    codec.set_slice_threads(8)
    if arm == "topos_kernel":
        mv = tb.ToposMovieFile(codec, path)
        dec = tb.ToposDecoder(codec)
        total = int(mv.info().sample_count)
        pkts = [mv.packet(i) for i in range(total)]
        p0 = dec.prepare(pkts[0])
        pc = int(p0.plane_count)
        geoms = [codec.plane_geometry(p0, p) for p in range(pc)]
        frame_views = []
        keep = []  # 平面数组保活：make_views 仅存地址，不持引用
        import numpy as np
        for i in range(total):
            arrs = [np.zeros((h, w), dtype="<u2") for (w, h) in geoms]
            keep.extend(arrs)
            frame_views.append(dec.make_views(arrs))
        def one_pass():
            t0 = time.perf_counter()
            for s in range(0, total, bs):
                dec.decode_views_batch(pkts[s:s+bs], frame_views[s:s+bs])
            return total / (time.perf_counter() - t0)
    else:  # topos_product：产品逐帧口径（packet_into + tc_frame_decode）
        mv = tb.ToposMovieFile(codec, path)
        total = int(mv.info().sample_count)
        info = tb._CFrameOutput()
        buf0, size0 = mv.packet_into(0)
        rc = int(codec._lib.tc_frame_decode(buf0, size0, None, None,
                                            ctypes.byref(info)))
        assert rc in (0, 1), rc
        ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
        keep = []
        for p in range(int(info.plane_count)):
            pw, ph = codec.plane_geometry(info, p)
            arr = (ctypes.c_uint16 * (pw * ph))()
            keep.append(arr)
            ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))
        def one_pass():
            t0 = time.perf_counter()
            for i in range(total):
                buf, size = mv.packet_into(i)
                rc2 = int(codec._lib.tc_frame_decode(
                    buf, size, ptrs, None, ctypes.byref(info)))
                assert rc2 in (0, 1), (rc2, i)
            return total / (time.perf_counter() - t0)
    one_pass()  # 预热
    fps = [one_pass() for _ in range(passes)]
    mv.close()
else:
    import av
    threads = 8
    ttype = "SLICE" if arm.endswith("slice") else "FRAME"
    def one_pass():
        with av.open(path) as cont:
            ctx = cont.streams.video[0].codec_context
            ctx.thread_count = threads
            ctx.thread_type = ttype
            t0 = time.perf_counter()
            for cnt, _f in enumerate(cont.decode(video=0)):
                if cnt + 1 >= frames:
                    break
            return frames / (time.perf_counter() - t0)
    one_pass()  # 预热
    fps = [one_pass() for _ in range(passes)]

print(json.dumps({"fps_runs": [round(x, 1) for x in fps],
                  "fps_median": round(statistics.median(fps), 1)}))
'''


def run_child(arm: str, path: str, frames: int, passes: int, bs: int) -> Dict[str, Any]:
    env = dict(os.environ)
    env["REPO"] = str(REPO)
    cmd = [sys.executable, "-c", CHILD, str(REPO), arm, path,
           str(frames), str(passes), str(bs)]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO))
    if proc.returncode != 0:
        raise RuntimeError(f"arm {arm} 子进程失败 rc={proc.returncode}\n{proc.stderr[-2000:]}")
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    out["spawn_s"] = round(time.perf_counter() - t0, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", choices=("2k", "4k"), required=True)
    ap.add_argument("--pairs", type=int, default=12)
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--arms", nargs="+", default=None,
                    help="臂子集（默认全 5 臂）；紧凑复核可只留 "
                         "topos_kernel prores_slice")
    args = ap.parse_args()
    spec = SPECS[args.res]
    arms = args.arms if args.arms else ARMS
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise SystemExit(f"未知臂 {unknown}；可选 {ARMS}")

    paths = {a: str(REPO / spec[k]) for a, k in [
        ("topos_kernel", "topos"), ("topos_product", "topos"),
        ("topos_kernel_std", "topos_std"),
        ("prores_slice", "prores"), ("prores_frame", "prores"),
        ("dnxhr_slice", "dnxhr")]}
    for p in paths.values():
        if not Path(p).is_file():
            raise SystemExit(f"缺少素材 {p}")

    pairs: List[Dict[str, Any]] = []
    for pi in range(args.pairs):
        order = arms if pi % 2 == 0 else list(reversed(arms))
        row: Dict[str, Any] = {"order": order[0] == arms[0]}
        load = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
        row["load"] = " ".join(load.split()[-3:])
        for arm in order:
            r = run_child(arm, paths[arm], spec["frames"], args.passes, spec["bs"])
            row[arm] = r
            print(f"pair {pi:2d} {'F' if row['order'] else 'R'} {arm:14s}"
                  f" median={r['fps_median']:7.1f} runs={r['fps_runs']}", flush=True)
        pairs.append(row)

    summary: Dict[str, Any] = {}
    for arm in arms:
        meds = [p[arm]["fps_median"] for p in pairs]
        ratios = ([p[arm]["fps_median"] / p[ANCHOR]["fps_median"]
                   for p in pairs] if ANCHOR in arms else [])
        summary[arm] = {
            "fps_median": round(statistics.median(meds), 1),
            "fps_iqr": [round(sorted(meds)[len(meds)//4], 1),
                        round(sorted(meds)[3*len(meds)//4], 1)],
            "vs_anchor_median": (round(statistics.median(ratios), 3)
                                 if ratios else None),
            "vs_anchor_per_pair": [round(x, 3) for x in ratios],
        }

    ts = time.strftime("%Y%m%d_%H%M%S")
    tag = "" if len(arms) == len(ARMS) else "_tight"
    out_dir = REPO / "bench_out" / "topos_vs_ref" / f"{ts}_quiet{args.res}{tag}"
    out_dir.mkdir(parents=True)
    result = {
        "meta": {
            "purpose": "ProRes/DNxHR 解码基线静机同窗重测（候选①：钉公平比值）",
            "res": args.res, "frames": spec["frames"], "bs": spec["bs"],
            "pairs": args.pairs, "passes": args.passes, "arms": arms,
            "threads_topos": 8, "threads_ref": 8,
            "anchor": ANCHOR,
            "python": sys.version.split()[0],
        },
        "pairs": pairs, "summary": summary,
    }
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n== {args.res} 静机成对中位（对 {ANCHOR} 比值）==")
    for arm in arms:
        s = summary[arm]
        ratio = f"  vs锚 {s['vs_anchor_median']:.3f}" if s["vs_anchor_median"] else ""
        tag_a = " ←锚" if arm == ANCHOR else ""
        print(f"  {arm:14s} {s['fps_median']:7.1f} fps{ratio}{tag_a}")
    print(f"→ {out_dir / 'result.json'}")


if __name__ == "__main__":
    main()
