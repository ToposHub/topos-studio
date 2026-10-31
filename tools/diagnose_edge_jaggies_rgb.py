#!/usr/bin/env python3
"""边缘锯齿取证 II：活动度-误差相关性 + 色度/RGB 观感仿真。

核心问题：同样大小下为什么 TOPOS 边缘有"锯齿感"而 ProRes 几乎没有？
本脚本量化两个机制：
  A. 空间自适应：Pearson(局部活动度, |量化误差|)——均匀 qp 下误差必然
     跟着边缘走（相关性高）；逐宏块自适应（ProRes）把误差推离边缘（低）。
  B. 色度路径：422→444 水平上采样 + BT.709 RGB 后，彩色边缘（陶罐沿等）
     的 RGB 误差与放大面板。
输出：bench_out/edge_jag/edge_jag_rgb_report.json + RGB panel PNG。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import laplace, sobel

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
    OUT_DIR,
    PRORES_NAME,
    SIZES,
    _plane_array,
    auto_crops,
)

MASTER = MEDIA / "4K_prores4444.mov"


def iter_reference_planes(fmt: str, w: int, h: int):
    with av.open(str(MASTER)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if (f.width, f.height) != (w, h) or f.format.name != fmt:
                f = f.reformat(width=w, height=h, format=fmt)
            yield i, tuple(_plane_array(p) for p in f.planes[:3])


def iter_prores_planes(path, fmt: str):
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if f.format.name != fmt:
                f = f.reformat(format=fmt)
            yield i, tuple(_plane_array(p) for p in f.planes[:3])


def iter_topos_planes(codec, path):
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    try:
        n = int(movie.info().sample_count)
        views = None
        planes = None
        for i in range(n):
            buf, size = movie.packet_into(i)
            fi = decoder.prepare(buf, size)
            if views is None:
                planes = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(planes)
            decoder.decode_views(buf, views, int(fi.plane_count), size)
            yield i, tuple(np.array(p) for p in planes[:3])
    finally:
        decoder.close()
        movie.close()


def grab_planes(source, want: set[int]) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    for i, fr in source:
        if i in want:
            out[i] = fr
        if len(out) == len(want) or i > max(want):
            break
    return out


def yuv_to_rgb(frames: np.ndarray) -> np.ndarray:
    """10-bit limited 422 (3,H,W) → RGB float32 0..255 (BT.709, 水平双线性)。"""
    y, u, v = [p.astype(np.float32) for p in frames]
    if u.shape[1] != y.shape[1]:  # 422 → 444
        cw = u.shape[1]
        u2 = np.empty((u.shape[0], y.shape[1]), np.float32)
        v2 = np.empty_like(u2)
        u2[:, 0::2] = u
        v2[:, 0::2] = v
        u2[:, 1::2][:, :-1] = (u[:, :-1] + u[:, 1:]) * 0.5
        v2[:, 1::2][:, :-1] = (v[:, :-1] + v[:, 1:]) * 0.5
        u2[:, -1] = u[:, -1]
        v2[:, -1] = v[:, -1]
        u, v = u2, v2
    yn = (y - 64.0) / 876.0
    un = (u - 512.0) / 896.0
    vn = (v - 512.0) / 896.0
    r = yn + 1.5748 * vn
    b = yn + 1.8556 * un
    g = (yn - 0.2126 * r - 0.0722 * b) / 0.7152
    rgb = np.stack([r, g, b], axis=0)
    return np.clip(rgb, 0.0, 1.0) * 255.0


def activity_error_corr(ref: np.ndarray, tst: np.ndarray) -> float:
    """Pearson(|laplace(ref)|, |err|)：均匀 qp → 高（误差贴着边缘）。"""
    act = np.abs(laplace(ref.astype(np.float64))).ravel()
    err = np.abs(tst.astype(np.float64) - ref.astype(np.float64)).ravel()
    if act.std() < 1e-9 or err.std() < 1e-9:
        return float("nan")
    return float(np.corrcoef(act, err)[0, 1])


def edge_psnr_rgb(ref: np.ndarray, tst: np.ndarray) -> dict:
    rl = ref.mean(axis=0)
    tl = tst.mean(axis=0)
    mag = np.hypot(sobel(rl, 0), sobel(rl, 1))
    mask = mag > max(20.0, float(np.percentile(mag, 96.0)))
    d = tl - rl
    mse_e = float(np.mean(d[mask] ** 2))
    mse_a = float(np.mean(d ** 2))
    psnr = lambda m: float("nan") if m <= 0 else 10.0 * np.log10(255.0**2 / m)
    return {
        "rgb_luma_psnr_edge": round(psnr(mse_e), 2),
        "rgb_luma_psnr_all": round(psnr(mse_a), 2),
        "rgb_mae_edge": round(float(np.mean(np.abs(d[mask]))), 2),
    }


def panel_rgb(ref: np.ndarray, prores: np.ndarray, topos: np.ndarray,
              y: int, x: int, win: int, zoom: int, out: Path) -> None:
    strips = [(ref, "Master RGB"), (prores, "ProRes RGB"), (topos, "TOPOS RGB")]
    label_h = 30
    canvas_w = sum(win * zoom for _ in strips) + 8 * 4
    canvas_h = win * zoom + label_h + 8
    canvas = Image.new("RGB", (canvas_w, canvas_h), (18, 18, 18))
    dr = ImageDraw.Draw(canvas)
    cx = 8
    for fr, name in strips:
        crop = fr[:, y:y + win, x:x + win].transpose(1, 2, 0)
        big = Image.fromarray(crop.astype(np.uint8)).resize(
            (win * zoom, win * zoom), Image.NEAREST)
        dr.text((cx + 4, 4), name, fill=(240, 240, 240))
        canvas.paste(big, (cx, label_h))
        cx += win * zoom + 8
    canvas.save(out)


def run_pair(size: str, tier: str, frames: list[int], n_crops: int,
             codec: ToposCodec) -> dict:
    w, h = SIZES[size]
    fmt = "yuv422p10le"
    want = set(frames)
    ref = grab_planes(iter_reference_planes(fmt, w, h), want)
    pr = grab_planes(iter_prores_planes(MEDIA / PRORES_NAME[tier].replace("2K_", f"{size}_", 1), fmt), want)
    tp = grab_planes(iter_topos_planes(codec, MEDIA / f"{size}_topos_{tier}.mov"), want)

    report: dict = {"size": size, "tier": tier, "frames": sorted(want),
                    "per_frame": {}, "mean": {}}
    acc = {"prores": {}, "topos": {}}
    for i in sorted(want):
        rgb_ref = yuv_to_rgb(ref[i])
        row: dict = {}
        for who, fr in (("prores", pr[i]), ("topos", tp[i])):
            rgb = yuv_to_rgb(fr)
            m = {
                "y_act_err_corr": round(activity_error_corr(ref[i][0], fr[0]), 3),
                "u_psnr": round(10 * np.log10(1023**2 / max(
                    float(np.mean((fr[1].astype(np.float64) - ref[i][1].astype(np.float64))**2)), 1e-9)), 2),
                "v_psnr": round(10 * np.log10(1023**2 / max(
                    float(np.mean((fr[2].astype(np.float64) - ref[i][2].astype(np.float64))**2)), 1e-9)), 2),
                **edge_psnr_rgb(rgb_ref, rgb),
            }
            row[who] = m
            for k, v in m.items():
                acc[who].setdefault(k, []).append(v)
        report["per_frame"][i] = row
        # RGB panel（第一帧）
        if i == sorted(want)[0]:
            for j, (cy, cx) in enumerate(auto_crops(ref[i][0], k=n_crops)):
                panel_rgb(rgb_ref, yuv_to_rgb(pr[i]), yuv_to_rgb(tp[i]),
                          cy, cx, win=192, zoom=5,
                          out=OUT_DIR / f"rgbpanel_{size}_{tier}_f{i}_c{j}.png")
    for who in ("prores", "topos"):
        report["mean"][who] = {k: round(float(np.mean(v)), 3)
                               for k, v in acc[who].items()}
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="2K", choices=list(SIZES))
    ap.add_argument("--tiers", nargs="+", default=["proxy", "standard"])
    ap.add_argument("--frames", nargs="+", type=int, default=[100, 300])
    ap.add_argument("--crops", type=int, default=3)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codec = ToposCodec()
    reports = []
    for tier in args.tiers:
        print(f"[edge-jag-rgb] {args.size}/{tier} …", flush=True)
        reports.append(run_pair(args.size, tier, args.frames, args.crops, codec))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "y_act_err_corr：Pearson(局部活动度,|量化误差|)——高=误差贴着边缘走"
                "（均匀 qp 签名）；u/v_psnr 为色度平面保真；rgb_luma_psnr_edge 为"
                "422→444 上采样 + BT.709 后的观感亮度边缘误差。",
        "pairs": reports,
    }
    out = OUT_DIR / "edge_jag_rgb_report.json"
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    for r in reports:
        print(f"\n== {r['size']}/{r['tier']} ==")
        for who in ("prores", "topos"):
            print(f"  {who:6s} {r['mean'][who]}")
    print(f"\nreport → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
