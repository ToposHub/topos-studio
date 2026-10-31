#!/usr/bin/env python3
"""边缘锯齿取证：同帧对齐 母版 / ProRes / TOPOS，定位"物体边缘锯齿感"的来源。

对每个 (size, tier) 对：
  1. 抽取同一帧（母版 reformat 到目标尺寸 = ground truth）；
  2. 边缘掩码（母版 Sobel 高分位 + 膨胀）；
  3. 边缘限定误差（PSNR/MAE）、锐度保持率（Laplacian 能量比）、
     8×8 栅格残留集中度（块对齐锯齿签名）、残差轴向各向异性（阶梯签名）；
  4. 自动选边缘密度最高的窗口，最近邻放大存三方对比图。

输出：bench_out/edge_jag/edge_jag_report.json + panel PNG。
用法：.venv/bin/python tools/diagnose_edge_jaggies.py --size 2K --tiers proxy standard
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
from scipy.ndimage import binary_dilation, laplace, sobel

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
)

MEDIA = REPO / "tests" / "test_videos"
MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR = REPO / "bench_out" / "edge_jag"
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}
TIERS = {
    "proxy": "yuv422p10le",
    "lt": "yuv422p10le",
    "standard": "yuv422p10le",
    "hq": "yuv422p10le",
    "4444": "yuv444p10le",
    "4444xq": "yuv444p12le",
}
PRORES_NAME = {
    "proxy": "2K_prores422Proxy.mov",
    "lt": "2K_prores422LT.mov",
    "standard": "2K_prores422.mov",
    "hq": "2K_prores422HQ.mov",
    "4444": "2K_prores4444.mov",
    "4444xq": "2K_prores4444XQ.mov",
}


def _plane_array(plane) -> np.ndarray:
    return np.ascontiguousarray(np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2)))


def iter_reference(fmt: str, w: int, h: int):
    """母版 → (index, Y)。2K 先 reformat（与 P0 证据链同口径 bicubic）。"""
    with av.open(str(MASTER)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if (f.width, f.height) != (w, h) or f.format.name != fmt:
                f = f.reformat(width=w, height=h, format=fmt)
            yield i, _plane_array(f.planes[0])


def iter_prores(path: Path, fmt: str):
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if f.format.name != fmt:
                f = f.reformat(format=fmt)
            yield i, _plane_array(f.planes[0])


def iter_topos(codec: ToposCodec, path: Path):
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
            yield i, planes[0]
    finally:
        decoder.close()
        movie.close()


def grab_y(source, want: set[int]) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    for i, y in source:
        if i in want:
            out[i] = np.array(y)
        if len(out) == len(want) or i > max(want):
            break
    return out


# ————————————————————————— 度量 —————————————————————————

def psnr(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    d = (a.astype(np.float64) - b.astype(np.float64))
    if mask is not None:
        d = d[mask]
    mse = float(np.mean(d * d)) if d.size else float("nan")
    return float("nan") if mse <= 0 else 10.0 * np.log10(1023.0**2 / mse)


def edge_mask(ref: np.ndarray) -> np.ndarray:
    f = ref.astype(np.float64)
    mag = np.hypot(sobel(f, 0), sobel(f, 1))
    thr = max(30.0, float(np.percentile(mag, 96.0)))
    return binary_dilation(mag > thr, iterations=1)


def lap_energy(y: np.ndarray, mask: np.ndarray) -> float:
    l = laplace(y.astype(np.float64))
    return float(np.mean(l[mask] ** 2)) if mask.any() else float("nan")


def grid_signature(res: np.ndarray, mask: np.ndarray) -> float:
    """残差能量落在 8×8 块界线上的集中度（≈1 无块对齐；>1.2 块状锯齿）。"""
    h, w = res.shape
    yy, xx = np.mgrid[0:h, 0:w]
    grid = (xx % 8 == 0) | (yy % 8 == 0)
    m = mask & ~grid
    g = mask & grid
    e_all = float(np.sum(res[mask] ** 2))
    if e_all <= 0 or not m.any() or not g.any():
        return float("nan")
    e_grid = float(np.sum(res[g] ** 2))
    area_frac = g.sum() / float(mask.sum())
    return (e_grid / e_all) / area_frac


def stair_signature(res: np.ndarray, mask: np.ndarray) -> float:
    """残差梯度的轴向占比：锯齿阶梯沿水平/垂直走块，即使边缘是斜的。"""
    gx = sobel(res, 1)
    gy = sobel(res, 0)
    mag = np.hypot(gx, gy)
    sel = mask & (mag > np.percentile(mag[mask], 70) if mask.any() else mask)
    if not sel.any():
        return float("nan")
    axis = np.minimum(np.abs(gx[sel]), np.abs(gy[sel]))
    tot = mag[sel]
    return float(np.sum(axis) / np.sum(tot))  # 0.5=各向同性；>0.6 轴向阶梯


def metrics_vs_ref(ref: np.ndarray, tst: np.ndarray, mask: np.ndarray) -> dict:
    res = tst.astype(np.float64) - ref.astype(np.float64)
    le_ref = lap_energy(ref, mask)
    return {
        "psnr_all": round(psnr(tst, ref), 2),
        "psnr_edge": round(psnr(tst, ref, mask), 2),
        "mae_edge": round(float(np.mean(np.abs(res[mask]))), 2),
        "sharpness_retention": round(lap_energy(tst, mask) / le_ref, 3),
        "grid_residual_ratio": round(grid_signature(res, mask), 3),
        "stair_axis_ratio": round(stair_signature(res, mask), 3),
    }


# ————————————————————————— 可视化 —————————————————————————

def auto_crops(ref: np.ndarray, k: int = 4, win: int = 192) -> list[tuple[int, int]]:
    f = ref.astype(np.float64)
    mag = np.hypot(sobel(f, 0), sobel(f, 1))
    dens = mag > np.percentile(mag, 96)
    h, w = dens.shape
    step = win // 2
    cands = []
    for y in range(0, h - win, step):
        for x in range(0, w - win, step):
            cands.append((float(dens[y:y + win, x:x + win].mean()), y, x))
    cands.sort(reverse=True)
    picked: list[tuple[int, int]] = []
    for d, y, x in cands:
        if all(abs(y - py) > win or abs(x - px) > win for py, px in picked):
            picked.append((y, x))
        if len(picked) == k:
            break
    return picked


def panel(ref: np.ndarray, prores: np.ndarray, topos: np.ndarray,
          y: int, x: int, win: int, zoom: int, out: Path, tag: str,
          labels: tuple[str, str, str] = ("Master (truth)", "ProRes", "TOPOS")
          ) -> None:
    """三方放大面板。labels 可改标题（如 A/B 变体的「滤波 / 关滤波」）——
    面板只画 ASCII，CJK 标题会缺字模。"""
    imgs = [(ref, labels[0]), (prores, labels[1]), (topos, labels[2])]
    label_h = 30
    strips = []
    for arr, name in imgs:
        crop = arr[y:y + win, x:x + win]
        u8 = np.clip(crop.astype(np.float64) / 4.0, 0, 255).astype(np.uint8)
        big = Image.fromarray(u8).resize((win * zoom, win * zoom), Image.NEAREST)
        strips.append((big, name))
    W = sum(s[0].width for s in strips) + 8 * (len(strips) + 1)
    H = strips[0][0].height + label_h + 16
    canvas = Image.new("RGB", (W, H), (18, 18, 18))
    dr = ImageDraw.Draw(canvas)
    cx = 8
    for big, name in strips:
        dr.text((cx + 4, 4), name, fill=(240, 240, 240))
        canvas.paste(big, (cx, label_h))
        cx += big.width + 8
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    _ = tag


# ————————————————————————— 主流程 —————————————————————————

def run_pair(size: str, tier: str, frames: list[int], n_crops: int,
             codec: ToposCodec) -> dict:
    w, h = SIZES[size]
    fmt = TIERS[tier]
    topos_path = MEDIA / f"{size}_topos_{tier}.mov"
    prores_path = MEDIA / PRORES_NAME[tier].replace("2K_", f"{size}_", 1)
    want = set(frames)

    ref = grab_y(iter_reference(fmt, w, h), want)
    pr = grab_y(iter_prores(prores_path, fmt), want)
    tp = grab_y(iter_topos(codec, topos_path), want)

    report = {"size": size, "tier": tier, "frames": sorted(want),
              "prores": prores_path.name, "topos": topos_path.name,
              "per_frame": {}, "mean": {}}
    acc = {"prores": {}, "topos": {}}
    for i in sorted(want):
        m = edge_mask(ref[i])
        row = {
            "edge_pct": round(100.0 * float(m.mean()), 1),
            "prores": metrics_vs_ref(ref[i], pr[i], m),
            "topos": metrics_vs_ref(ref[i], tp[i], m),
        }
        report["per_frame"][i] = row
        for who in ("prores", "topos"):
            for k, v in row[who].items():
                acc[who].setdefault(k, []).append(v)

    for who in ("prores", "topos"):
        report["mean"][who] = {k: round(float(np.mean(v)), 3)
                               for k, v in acc[who].items()}

    # 可视化：取第一个请求帧（边缘现象代表性最强）
    i0 = sorted(want)[0]
    m = edge_mask(ref[i0])
    for j, (cy, cx) in enumerate(auto_crops(ref[i0], k=n_crops)):
        panel(ref[i0], pr[i0], tp[i0], cy, cx, win=192, zoom=5,
              out=OUT_DIR / f"panel_{size}_{tier}_f{i0}_c{j}.png",
              tag=f"{size}/{tier} f{i0} crop{j} ({cx},{cy})")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="2K", choices=list(SIZES))
    ap.add_argument("--tiers", nargs="+", default=["proxy", "standard"])
    ap.add_argument("--frames", nargs="+", type=int, default=[100, 300])
    ap.add_argument("--crops", type=int, default=4)
    ap.add_argument("--json", default=str(OUT_DIR / "edge_jag_report.json"))
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codec = ToposCodec()
    reports = []
    for tier in args.tiers:
            print(f"[edge-jag] {args.size}/{tier} …", flush=True)
            reports.append(run_pair(args.size, tier, args.frames,
                                    args.crops, codec))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "边缘锯齿取证：ref=母版 reformat（bicubic）；grid_residual_ratio>1.2 "
                "表示误差集中在 8×8 块界；stair_axis_ratio>0.6 表示残差轴向化（阶梯）；"
                "sharpness_retention<1 表示边缘锐度损失。",
        "pairs": reports,
    }
    out = Path(args.json)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    for r in reports:
        print(f"\n== {r['size']}/{r['tier']} ==")
        for who in ("prores", "topos"):
            print(f"  {who:6s} {r['mean'][who]}")
    print(f"\nreport → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
