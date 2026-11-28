#!/usr/bin/env python3
"""Export matched-pixel 2× crops and same-scale luma error maps."""
from __future__ import annotations

import json
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from measure import OUT, SOURCES, WIDTH, HEIGHT, av_frames, source_path
from perceptual import candidate

FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
TAGS = ("prores_apple", "topos_default", "topos_matched_full")
LABELS = ("DNxHR HQX reference", "Apple ProRes 422 Proxy",
          "Topos 422 Proxy default", "Topos 422 Proxy rate-matched")
PATCH_W, PATCH_H = 156, 100


def psnr(sse: float, pixels: int) -> float:
    return 10 * math.log10(1023**2 * pixels / max(sse, 1))


def rgb_crop(planes, x: int, y: int, w: int, h: int) -> Image.Image:
    yc = (planes[0][y:y+h, x:x+w].astype(np.float32) - 64) / 876
    cb, cr = (np.repeat((p[y:y+h, x//2:(x+w)//2].astype(np.float32) - 512) / 896,
                        2, axis=1) for p in planes[1:])
    rgb = np.stack((yc + 1.5748 * cr, yc - 0.187324 * cb - 0.468124 * cr,
                    yc + 1.8556 * cb), axis=-1)
    return Image.fromarray(np.uint8(np.round(np.clip(rgb, 0, 1) * 255)))


def error_crop(reference, output, x: int, y: int, w: int, h: int) -> Image.Image:
    ref = reference[0][y:y+h, x:x+w].astype(np.int32)
    diff = np.abs(output[0][y:y+h, x:x+w].astype(np.int32) - ref)
    return Image.fromarray(np.uint8(np.minimum(diff * (255 / 32), 255)))


def region(clip: str, index: int) -> dict:
    _, fps, _ = SOURCES[clip]
    iters = [av_frames(source_path(clip), [(index, 1)], fps)]
    iters.extend(candidate(clip, tag, [(index, 1)]) for tag in TAGS)
    try:
        planes = [next(it)[1] for it in iters]
        ref, prores, top, _ = planes
        luma_ref = ref[0].astype(np.int32)
        excess = (top[0].astype(np.int32) - luma_ref)**2 - (
            prores[0].astype(np.int32) - luma_ref)**2
        # Choose the largest mean excess on a fixed 156×100 crop grid.
        grid_w, grid_h = WIDTH // PATCH_W, HEIGHT // PATCH_H
        means = excess[:grid_h*PATCH_H, :grid_w*PATCH_W].reshape(
            grid_h, PATCH_H, grid_w, PATCH_W).mean(axis=(1, 3))
        row, col = np.unravel_index(np.argmax(means), means.shape)
        x, y = int(col) * PATCH_W, int(row) * PATCH_H
        ir = ref[0][y:y+PATCH_H, x:x+PATCH_W].astype(np.int32)
        crop_scores = [
            psnr(np.sum(np.square(ir - p[0][y:y+PATCH_H, x:x+PATCH_W])), ir.size)
            for p in planes[1:]
        ]
        return {
            "index": index, "x": x, "y": y, "width": PATCH_W, "height": PATCH_H,
            "patch_y_psnr": crop_scores,
            "images": [rgb_crop(p, x, y, PATCH_W, PATCH_H) for p in planes],
            "errors": [error_crop(ref, p, x, y, PATCH_W, PATCH_H) for p in planes],
        }
    finally:
        for it in iters:
            it.close()


def draw(clip: str) -> None:
    pro, top, matched = [
        json.loads((OUT / f"{clip}_{tag}.score.json").read_text()) for tag in TAGS
    ]
    gaps = sorted((10 * math.log10(p["sse"][0] / t["sse"][0]), t["frame"])
                  for p, t in zip(pro, top, strict=True))
    selected = [(gaps[0], "Largest frame-level luma PSNR deficit"),
                (gaps[len(gaps)//2], "Median frame-level luma PSNR difference")]
    regions = [(region(clip, i), float(gap), caption) for (gap, i), caption in selected]
    panel = Image.new("RGB", (1336, 876), "#121820")
    ink = ImageDraw.Draw(panel)
    title = ImageFont.truetype(BOLD, 26)
    heading = ImageFont.truetype(BOLD, 17)
    note = ImageFont.truetype(FONT, 14)
    label = "Beihai Park" if clip == "beihai" else "Yuanmingyuan"
    ink.text((24, 16), f"{label} | UHD 10-bit 4:2:2 Proxy comparison", font=title, fill="#f0f5fc")
    ink.text((24, 50), "Each row uses the same source frame and coordinates. Crops enlarged 2×.",
             font=note, fill="#b8c9da")
    for col, name in enumerate(LABELS):
        ink.text((24 + col*327, 86), name, font=heading, fill="#f0f5fc")
    for slot, (r, gap, caption) in enumerate(regions):
        top_y = 116 + 372*slot
        index, x, y = r["index"], r["x"], r["y"]
        ink.text((24, top_y), f'{caption} ({gap:+.2f} dB)  |  Frame {index}  |  Crop x={x}, y={y}',
                 font=note, fill="#56d8ff")
        for col, (thumb, error) in enumerate(zip(r.pop("images"), r.pop("errors"), strict=True)):
            xcol = 24 + col*327
            panel.paste(thumb.resize((312, 200), Image.Resampling.NEAREST), (xcol, top_y + 22))
            panel.paste(error.resize((312, 200), Image.Resampling.NEAREST).crop((0, 40, 312, 140)),
                        (xcol, top_y + 247))
            scores = r["patch_y_psnr"]
            desc = ("Luma error reference" if col == 0 else
                    f"Crop Y-PSNR {scores[col-1]:.2f} dB | absolute luma error strip")
            ink.text((xcol, top_y + 227), desc, font=note, fill="#b8c9da")
    ink.text((24, 854), "Error scale: 0 code values = black, 32 or more = white. Frame numbering starts at 0.",
             font=note, fill="#b8c9da")
    panel.save(OUT / f"{clip}_comparisons.png")
    (OUT / f"{clip}_comparisons.json").write_text(json.dumps([
        dict(r, default_minus_prores_frame_y_db=gap, selection=caption)
        for r, gap, caption in regions
    ], indent=2) + "\n")


if __name__ == "__main__":
    for clip in SOURCES:
        draw(clip)
