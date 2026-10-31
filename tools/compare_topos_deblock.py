#!/usr/bin/env python3
"""去块滤波前后对比（同一码流）：画质指标 + 解码吞吐 + 三方放大面板。

滤波是纯解码端，故"前后"只换库（``--old-lib`` / ``--new-lib``）不换码流：
同一进程内分别加载两个 dylib 解码同一文件，指标与面板同源可比。解码吞吐
取多轮**中位数**（口径：默认线程池满速，全片解码）。

用法::

    .venv/bin/python tools/compare_topos_deblock.py \
      --file bench_out/qgain/base_2K_proxy.mov --frames 240
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from bench_topos_qgain import (  # noqa: E402
    _blockiness, _edge_mask, _grid, _psnr, _sharp, _stair, iter_master,
)
from diagnose_edge_jaggies import auto_crops, panel  # noqa: E402
from scipy.ndimage import laplace  # noqa: E402

SAMPLE = (0, 100, 240, 300, 486)
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}


def decode_all(lib_path: str, file: str, want: set) -> tuple[dict, float]:
    """全片解码一次：返回 ({frame: Y}, 帧率)。每轮都新建 codec/decoder。"""
    from src.shared.codec.topos_binding import (
        ToposCodec, ToposDecoder, ToposMovieFile, load_library,
    )
    codec = ToposCodec(load_library(lib_path))
    movie = ToposMovieFile(codec, file)
    decoder = ToposDecoder(codec)
    got: dict[int, np.ndarray] = {}
    views = None
    n = 0
    t0 = time.perf_counter()
    try:
        total = int(movie.info().sample_count)
        for i in range(total):
            buf, size_b = movie.packet_into(i)
            fi = decoder.prepare(buf, size_b)
            if views is None:
                planes = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(planes)
            decoder.decode_views(buf, views, int(fi.plane_count), size_b)
            if i in want:
                got[i] = np.array(planes[0])
            n += 1
    finally:
        dt = time.perf_counter() - t0
        decoder.close()
        movie.close()
    return got, (n / dt if dt > 0 else float("nan"))


def metrics(ref: dict, got: dict) -> dict:
    acc: dict[str, list] = {}
    for i in sorted(ref):
        if i not in got:
            continue
        ry, tp = ref[i][0], got[i]
        f = ry.astype(np.float64)
        mask = _edge_mask(ry)
        ref_lap = float(np.mean(laplace(f)[mask] ** 2))
        res = tp.astype(np.float64) - f
        (bh, _), (bv, _) = _blockiness(ry, tp)
        row = {"block_h": bh, "block_v": bv, "y_psnr": _psnr(ry, tp),
               "y_psnr_edge": _psnr(ry, tp, mask),
               "sharp": _sharp(tp, mask, ref_lap),
               "grid": _grid(res, mask), "stair": _stair(res, mask)}
        for k, v in row.items():
            acc.setdefault(k, []).append(v)
    return {k: round(float(np.mean(v)), 4) for k, v in acc.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--old-lib", required=True)
    ap.add_argument("--new-lib", required=True)
    ap.add_argument("--size", default="2K", choices=tuple(SIZES),
                    help="参照母版 reformat 目标尺寸（与 --file 同分辨率）")
    ap.add_argument("--frame", type=int, default=240, help="面板帧")
    ap.add_argument("--crops", type=int, default=3)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", type=Path,
                    default=REPO / "bench_out" / "qgain")
    args = ap.parse_args()

    want = set(SAMPLE)
    ref = iter_master("yuv422p10le", *SIZES[args.size], want)

    res: dict = {}
    for tag, lib in (("old", args.old_lib), ("new", args.new_lib)):
        fps = []
        got = None
        for _ in range(args.reps):
            got, f = decode_all(lib, args.file, want)
            fps.append(f)
        res[tag] = {"lib": lib, "dec_fps_median": round(float(np.median(fps)), 1),
                    "dec_fps_all": [round(x, 1) for x in fps],
                    **metrics(ref, got)}
        if got is not None and args.frame in got:
            res[tag]["_y"] = got[args.frame]

    keys = ("block_h", "block_v", "y_psnr", "y_psnr_edge", "sharp", "grid",
            "stair", "dec_fps_median")
    print(f"\n{'指标':<16}{'改动前':>13}{'改动后':>13}{'变化':>13}")
    for k in keys:
        a, b = res["old"].get(k), res["new"].get(k)
        if isinstance(a, float) and isinstance(b, float):
            print(f"{k:<16}{a:>13.4f}{b:>13.4f}{b - a:>+13.4f}")
        else:
            print(f"{k:<16}{str(a):>13}{str(b):>13}{'':>13}")

    if args.crops > 0 and "_y" in res["old"] and "_y" in res["new"]:
        args.out.mkdir(parents=True, exist_ok=True)
        f0 = args.frame
        for j, (cy, cx) in enumerate(auto_crops(ref[f0][0], k=args.crops)):
            panel(ref[f0][0], res["old"]["_y"], res["new"]["_y"], cy, cx, 160, 6,
                  args.out / f"deblock_f{f0}_c{j}.png",
                  f"{Path(args.file).stem} f{f0} crop{j}")
        print(f"面板 → {args.out}/deblock_f{f0}_c*.png")

    out = args.out / f"deblock_cmp_{Path(args.file).stem}.json"
    for t in ("old", "new"):
        res[t].pop("_y", None)
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print(f"report → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
