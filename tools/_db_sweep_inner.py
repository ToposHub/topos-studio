#!/usr/bin/env python3
"""sweep_topos_deblock.py 的内层：单个滤波设置 → 一行指标 JSON（stdout）。

环境变量由外层设置（TOPOS_CODEC_LIB 指向调参库 + DB_* 旋钮）。
"""
from __future__ import annotations

import argparse
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
    _blockiness, _edge_mask, _grid, _psnr, _sharp, _stair,
    iter_master, iter_topos,
)
from scipy.ndimage import laplace  # noqa: E402

FRAMES = (0, 100, 240, 300, 486)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--name", default="?")
    ap.add_argument("--snap", type=Path, default=None)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()

    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.codec.topos_binding import ToposDecoder, ToposMovieFile

    want = set(FRAMES)
    ref = iter_master("yuv422p10le", args.width, args.height, want)

    # 全片解码计时（口径与 bench_topos_speed 同：默认线程池满速），
    # 顺带抽取抽样帧做画质——避免"只解 5 帧"的计时噪声。
    codec = ToposCodec()
    movie = ToposMovieFile(codec, str(args.file))
    decoder = ToposDecoder(codec)
    views = None
    planes = None
    got: dict[int, np.ndarray] = {}
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
        dec_s = time.perf_counter() - t0
        decoder.close()
        movie.close()
    tst = {i: (got[i],) for i in got}

    acc: dict[str, list] = {}
    snap = None
    for i in sorted(ref):
        if i not in got:
            continue
        ry, tp = ref[i][0], got[i]
        f = ry.astype(np.float64)
        mask = _edge_mask(ry)
        ref_lap = float(np.mean(laplace(f)[mask] ** 2))
        res = tp.astype(np.float64) - f
        (bh, _), (bv, _) = _blockiness(ry, tp)
        row = {
            "block_h": bh, "block_v": bv,
            "y_psnr": _psnr(ry, tp), "y_psnr_edge": _psnr(ry, tp, mask),
            "sharp": _sharp(tp, mask, ref_lap),
            "grid": _grid(res, mask), "stair": _stair(res, mask),
        }
        for k, v in row.items():
            acc.setdefault(k, []).append(v)
        if snap is None and i == 240:
            snap = np.array(tp)
    if not acc:
        raise SystemExit("无可比帧：码流与抽样帧不匹配？")
    out = {k: round(float(np.mean(v)), 4) for k, v in acc.items()}
    out["dec_fps"] = round(n / dec_s, 1) if dec_s > 0 else None
    out["file"] = Path(args.file).name
    if snap is not None and args.snap:
        args.snap.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(args.snap).replace(".json", f"_{args.name}.npy"), snap)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
