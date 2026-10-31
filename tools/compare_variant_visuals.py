#!/usr/bin/env python3
"""变体视觉对照：母版 / ProRes / v0 / 变体 同窗口 RGB 最近邻放大 + 绝对残差。

用法：
  .venv/bin/python tools/compare_variant_visuals.py \
      --size 2K --tier standard --variants v0 v3 v4 --frames 100 --crops 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import sobel

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
)
from diagnose_edge_jaggies import (  # noqa: E402
    MEDIA,
    PRORES_NAME,
    SIZES,
    _plane_array,
    auto_crops,
)
from diagnose_edge_jaggies_rgb import (  # noqa: E402
    iter_reference_planes,
    iter_prores_planes,
    yuv_to_rgb,
)
from bench_topos_variant import OUT_DIR  # noqa: E402

MASTER = MEDIA / "4K_prores4444.mov"


def iter_variant_planes(codec, path: Path, w: int, h: int, fmt: str, idx: int):
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    views = None
    planes = None
    try:
        buf, size_b = movie.packet_into(idx)
        fi = decoder.prepare(buf, size_b)
        planes = []
        for k in range(int(fi.plane_count)):
            pw, ph = codec.plane_geometry(fi, k)
            planes.append(np.empty((ph, pw), dtype="<u2"))
        views = decoder.make_views(planes)
        decoder.decode_views(buf, views, int(fi.plane_count), size_b)
        return tuple(np.array(p) for p in planes[:3])
    finally:
        decoder.close()
        movie.close()


def grab_one(source, idx: int):
    for i, fr in source:
        if i == idx:
            return fr
        if i > idx:
            break
    raise RuntimeError(f"frame {idx} not found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="2K")
    ap.add_argument("--tier", default="standard")
    ap.add_argument("--variants", nargs="+", default=["v0", "v3"])
    ap.add_argument("--frames", nargs="+", type=int, default=[100])
    ap.add_argument("--crops", type=int, default=3)
    ap.add_argument("--zoom", type=int, default=5)
    args = ap.parse_args()

    w, h = SIZES[args.size]
    fmt = "yuv422p10le"
    prores_path = MEDIA / PRORES_NAME[args.tier].replace("2K_", f"{args.size}_", 1)
    codec = ToposCodec()
    try:
        for fi in args.frames:
            ref = grab_one(iter_reference_planes(fmt, w, h), fi)
            pr = grab_one(iter_prores_planes(prores_path, fmt), fi)
            variants = {
                v: iter_variant_planes(
                    codec, OUT_DIR / f"{v}_{args.size}_{args.tier}.mov", w, h, fmt, fi)
                for v in args.variants
            }
            rgb_ref = yuv_to_rgb(ref)
            # 绝对残差（Y 平面，全帧）
            res_stats = {
                v: round(float(np.mean(np.abs(
                    variants[v][0].astype(np.float64) - ref[0].astype(np.float64)))), 2)
                for v in args.variants
            }
            pr_res = round(float(np.mean(np.abs(
                pr[0].astype(np.float64) - ref[0].astype(np.float64)))), 2)
            print(f"frame {fi}: mean|resY| prores={pr_res} variants={res_stats}")
            crops = auto_crops(ref[0], k=args.crops)
            for j, (cy, cx) in enumerate(crops):
                strips = [(rgb_ref, "Master"), (yuv_to_rgb(pr), "ProRes")]
                strips += [(yuv_to_rgb(variants[v]), v.upper()) for v in args.variants]
                win = 192
                zoom = args.zoom
                label_h = 28
                W = sum(win * zoom for _ in strips) + 8 * (len(strips) + 1)
                H = win * zoom + label_h + 8
                canvas = Image.new("RGB", (W, H), (18, 18, 18))
                dr = ImageDraw.Draw(canvas)
                cxp = 8
                for fr, name in strips:
                    crop = fr[:, cy:cy + win, cx:cx + win].transpose(1, 2, 0)
                    big = Image.fromarray(crop.astype(np.uint8)).resize(
                        (win * zoom, win * zoom), Image.NEAREST)
                    dr.text((cxp + 4, 4), name, fill=(240, 240, 240))
                    canvas.paste(big, (cxp, label_h))
                    cxp += win * zoom + 8
                out = OUT_DIR / f"vis_{args.size}_{args.tier}_f{fi}_c{j}.png"
                canvas.save(out)
                print(f"  panel → {out.name}  crop@({cx},{cy})")
    finally:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
