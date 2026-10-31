#!/usr/bin/env python3
"""去块滤波主观验收：平滑区裁片（块效应最显眼处）+ 误差放大图。

块效应在**平坦/渐变区**最容易被眼睛抓到，而 edge-density 选片器专挑线条
最密处（那里反而看不见）。本工具按"低梯度但非恒定"选片，并额外输出
|重建−参照|×增益 的误差图，让 8×8 网格直接显形。

用法::

    .venv/bin/python tools/panel_topos_blockiness.py \
      --file bench_out/qgain/base_2K_proxy.mov \
      --old-lib /tmp/topos_old/.../topos_codec.dylib \
      --new-lib native/topos_codec/build/release/topos_codec.dylib
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import sobel

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from bench_topos_qgain import iter_master  # noqa: E402
from compare_topos_deblock import decode_all  # noqa: E402


def pick_worst_blocking(ref_y: np.ndarray, dec_y: np.ndarray, k: int,
                        win: int, grad_max: float = 25.0,
                        mean_range: tuple[float, float] = (300.0, 820.0)
                        ) -> list[tuple[int, int, float]]:
    """选"旧解码里块界台阶最大"的窗口——让数据自己指出块效应在哪。

    只排除过曝/过暗与强纹理窗（那里本来就不看块）；评分 = 窗内跨 8×8
    块界的相邻像素差 − 块内相邻像素差（与全局 block_h 同定义）。
    """
    f = ref_y.astype(np.float64)
    d = dec_y.astype(np.float64)
    mag = np.hypot(sobel(f, 0), sobel(f, 1))
    dh = np.abs(np.diff(d, axis=1))
    dv = np.abs(np.diff(d, axis=0))
    h, w = mag.shape
    step = win // 2
    cols = np.arange(w - 1)
    rows = np.arange(h - 1)
    on_h = ((cols + 1) % 8 == 0)
    on_v = ((rows + 1) % 8 == 0)
    cands = []
    for y in range(0, h - win, step):
        for x in range(0, w - win, step):
            if not (mean_range[0] < f[y:y + win, x:x + win].mean() < mean_range[1]):
                continue
            if mag[y:y + win, x:x + win].mean() > grad_max:
                continue
            bh = dh[y:y + win, x:x + win - 1]
            bv = dv[y:y + win - 1, x:x + win]
            oh = on_h[x:x + win - 1]
            ov = on_v[y:y + win - 1]
            sb = (bh[:, oh].mean() - bh[:, ~oh].mean()
                  + bv[ov, :].mean() - bv[~ov, :].mean())
            cands.append((float(sb), y, x))
    cands.sort(reverse=True)
    picked: list[tuple[int, int, float]] = []
    for sc, y, x in cands:
        if all(abs(y - py) > win or abs(x - px) > win for _, py, px in picked):
            picked.append((sc, y, x))
        if len(picked) == k:
            break
    return picked


def strip(arr: np.ndarray, y: int, x: int, win: int, zoom: int,
          gain: float = 4.0, label: str = "") -> tuple[Image.Image, str]:
    crop = arr[y:y + win, x:x + win].astype(np.float64) * gain
    u8 = np.clip(crop, 0, 255).astype(np.uint8)
    return Image.fromarray(u8).resize((win * zoom, win * zoom), Image.NEAREST), label


def compose(sections: list[tuple[str, list[tuple[Image.Image, str]]]],
            out: Path) -> None:
    lab_h, pad = 26, 8
    width = 0
    for _, items in sections:
        width = max(width, sum(i[0].width for i in items) + pad * (len(items) + 1))
    height = sum(i[0][0].height + lab_h + pad for _, i in sections
                 if i) + lab_h * len(sections)
    canvas = Image.new("RGB", (width, height), (16, 16, 16))
    dr = ImageDraw.Draw(canvas)
    cy = 0
    for title, items in sections:
        dr.text((pad, cy + 6), title, fill=(255, 210, 120))
        cy += lab_h
        cx = pad
        for img, lab in items:
            canvas.paste(img, (cx, cy))
            dr.text((cx + 2, cy - 14), lab, fill=(230, 230, 230))
            cx += img.width + pad
        cy += (items[0][0].height if items else 0) + pad
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--old-lib", required=True)
    ap.add_argument("--new-lib", required=True)
    ap.add_argument("--frame", type=int, default=240)
    ap.add_argument("--win", type=int, default=128)
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--crops", type=int, default=2)
    ap.add_argument("--grad-lo", type=float, default=1.0)
    ap.add_argument("--grad-hi", type=float, default=25.0)
    ap.add_argument("--out", type=Path, default=REPO / "bench_out" / "qgain")
    args = ap.parse_args()

    want = {args.frame}
    ref = iter_master("yuv422p10le", 1920, 1080, want)[args.frame][0]
    old, _ = decode_all(args.old_lib, args.file, want)
    new, _ = decode_all(args.new_lib, args.file, want)
    oy, ny = old[args.frame], new[args.frame]
    mo = float(np.mean(np.abs(oy.astype(np.float64) - ref.astype(np.float64))))
    mn = float(np.mean(np.abs(ny.astype(np.float64) - ref.astype(np.float64))))
    print(f"帧 {args.frame}:  |old-ref| 均值 {mo:.3f}  |new-ref| 均值 {mn:.3f}")

    crops = pick_worst_blocking(ref, oy, args.crops, args.win, args.grad_hi)
    if not crops:
        print(f"帧 {args.frame} 无梯度∈[{args.grad_lo},{args.grad_hi}] 的 "
              f"{args.win}px 窗口（该帧过锐）——换 --frame 或放宽阈值")
    for j, (sc, cy, cx) in enumerate(crops):
        secs = [
            (f"帧 {args.frame} 裁片 ({cx},{cy}) 旧解码块界台阶 {sc:.2f} LSB"
             f" — ×{args.zoom} 放大",
             [strip(ref, cy, cx, args.win, args.zoom, 255.0 / 1023.0,
                    "母版(参照)"),
              strip(oy, cy, cx, args.win, args.zoom, 255.0 / 1023.0,
                    "改动前 v1"),
              strip(ny, cy, cx, args.win, args.zoom, 255.0 / 1023.0,
                    "改动后 v2")]),
            ("量化误差 |重建−母版| ×8（8×8 网格在此显形）",
             [strip(np.abs(oy.astype(np.float64) - ref.astype(np.float64)),
                    cy, cx, args.win, args.zoom, 8.0, "改动前 v1 误差"),
              strip(np.abs(ny.astype(np.float64) - ref.astype(np.float64)),
                    cy, cx, args.win, args.zoom, 8.0, "改动后 v2 误差")]),
        ]
        compose(secs, args.out / f"blockiness_f{args.frame}_c{j}.png")
        print(f"面板 → {args.out}/blockiness_f{args.frame}_c{j}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
