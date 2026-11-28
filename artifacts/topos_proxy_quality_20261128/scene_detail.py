#!/usr/bin/env python3
"""Compare the registered details from the three user-selected screenshot scenes."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from frequency import BANDS
from frequency import coefficients
from make_comparisons import FONT, BOLD, rgb_crop
from measure import OUT, SOURCES, WIDTH, HEIGHT, av_frames, source_path
from perceptual import candidate

SCENES = {
    "courtyard": {
        "name": "Courtyard", "frames": (260, 64), "frame": 301,
        "roi": (1120, 656, 1696, 912),
        "crops": [("Roof tiles and branches", 1872, 816),
                  ("Painted beams and window lattice", 1952, 1008),
                  ("Wall texture and fine branches", 1976, 1288)],
    },
    "eaves": {
        "name": "Painted eaves", "frames": (1446, 58), "frame": 1474,
        "roi": (1480, 336, 1248, 1328),
        "crops": [("Upper painted brackets", 2400, 840),
                  ("Glazed roof tile ridges", 1976, 1064),
                  ("Ornament under the lower eave", 2176, 1384)],
    },
    "gate": {
        "name": "Glazed gate", "frames": (1574, 58), "frame": 1604,
        "roi": (672, 192, 2496, 1552),
        "crops": [("Green and gold horizontal frieze", 1480, 454),
                  ("Ornate panels and red-painted doorway", 1680, 912),
                  ("Fine lattice and repeated red-gold borders", 1750, 1184)],
    },
}
CROP_W, CROP_H = 150, 92
CLIP = "beihai"
RATE = SOURCES[CLIP][1]
FULL = (0, 0, WIDTH, HEIGHT)
PANEL_TAGS = ("prores_apple", "topos_default")
PANEL_LABELS = ("DNxHR HQX reference", "Apple ProRes 422 Proxy",
                "Topos 422 Proxy default", "Topos 422 Proxy same scene bitrate",
                "Topos experimental AC dead zone 0")


def plane_region(plane, pos, chroma=False):
    x, y, w, h = pos
    return plane[y:y+h, x//2:(x+w)//2] if chroma else plane[y:y+h, x:x+w]


def sse(ref, out, pos):
    return [cv2.norm(plane_region(a, pos, i > 0), plane_region(b, pos, i > 0), cv2.NORM_L2SQR)
            for i, (a, b) in enumerate(zip(ref, out, strict=True))]


def to_psnr(mean_sse, pos):
    _, _, width, height = pos
    return (10 * np.log10(1023**2 / (mean_sse / (width * height) * [1, 2, 2]))).tolist()


def render(scene, reference, candidates) -> None:
    panel = Image.new("RGB", (1628, 1064), "#101720")
    d = ImageDraw.Draw(panel)
    title = ImageFont.truetype(BOLD, 27)
    heading = ImageFont.truetype(BOLD, 17)
    note = ImageFont.truetype(FONT, 15)
    tag = scene["tag"]
    index = scene["frame"]
    d.text((24, 19), f'{scene["name"]} | UHD DNxHR frame {index} at {index/25:.2f} s',
           font=title, fill="#f0f5fc")
    d.text((24, 55), "Exact same pixel coordinates in each column. Detail crops shown at 2×.",
           font=note, fill="#b9cada")
    thumbnail = rgb_crop(reference, *FULL).resize((464, 261), Image.Resampling.LANCZOS)
    panel.paste(thumbnail, (24, 89))
    colors = ["#51dcff", "#ffca52", "#ff779a"]
    for (label, cx, cy), color in zip(scene["crops"], colors, strict=True):
        coords = [24 + round(cx/3840*464), 89 + round(cy/2160*261),
                  24 + round((cx+CROP_W)/3840*464), 89 + round((cy+CROP_H)/2160*261)]
        d.rectangle(coords, outline=color, width=2)
    r = scene["roi"]
    for offset, ((label, x, y), color) in enumerate(zip(scene["crops"], colors, strict=True)):
        d.text((520, 117+40*offset), f"{label}   UHD x={x} y={y}", font=heading, fill=color)
    d.text((520, 255), f"Scored region {r[2]} × {r[3]} pixels at x={r[0]} y={r[1]}.",
           font=note, fill="#b9cada")
    d.text((520, 282), "Same-scene bitrate uses the Apple video payload in this window as its target.",
           font=note, fill="#b9cada")
    for x, label in enumerate(PANEL_LABELS):
        d.text((24+x*320, 374), label, font=heading, fill="#f0f5fc")
    for row, ((label, x, y), color) in enumerate(zip(scene["crops"], colors, strict=True)):
        top = 405 + row*212
        d.text((24, top), label, font=note, fill=color)
        for col, planes in enumerate((reference, *candidates)):
            patch = rgb_crop(planes, x, y, CROP_W, CROP_H)
            panel.paste(patch.resize((CROP_W*2,CROP_H*2), Image.Resampling.NEAREST),
                        (24+col*320, top+19))
    panel.save(OUT/f"{tag}_details.png")


def compare(tag: str) -> None:
    scene = dict(SCENES[tag], tag=tag)
    start, count = scene["frames"]
    idx = scene["frame"]
    span = [(start, count)]
    tags = ("prores_apple", "topos_default", "topos_matched_full", f"{tag}_matched", f"{tag}_dz0")
    info = {}
    for name in tags:
        suffix = "score.json"
        rows = json.loads((OUT / f"{CLIP}_{name}.{suffix}").read_text())
        selected = [row for row in rows if start <= row["frame"] < start+count]
        assert len(selected) == count
        info[name] = {"megabits_per_second": sum(r["packet_bytes"] for r in selected)
                      * 8*float(RATE)/count/1e6,
                      "full_psnr_yuv": to_psnr(np.array([r["sse"] for r in selected]).mean(0), FULL)}
    payload = []
    streams = [av_frames(source_path(CLIP), span, RATE)]
    streams.extend(candidate(CLIP, name, span) for name in tags)
    location = scene["roi"]
    mid = BANDS["mid_3_6"]
    try:
        for row in zip(*streams, strict=True):
            index, ref = row[0]
            all_out = [entry[1] for entry in row[1:]]
            ref_coeffs = coefficients([plane_region(ref[0], location)])[..., mid]
            denom = float(np.sum(ref_coeffs ** 2, dtype=np.float64))
            scores = []
            for out in all_out:
                coeffs = coefficients([plane_region(out[0], location)])[..., mid]
                cross = float(np.sum(ref_coeffs * coeffs, dtype=np.float64))
                scores.append({"sse": sse(ref,out,location), "mid_cross":cross, "mid_ref2":denom})
            payload.append({"frame":index, "codecs":dict(zip(tags,scores,strict=True))})
            if index == idx:
                render(scene, ref, [all_out[i] for i in (0, 1, 3, 4)])
    finally:
        for generator in streams:
            generator.close()
    assert len(payload) == count
    for name in tags:
        rows = [row["codecs"][name] for row in payload]
        info[name]["region_psnr_yuv"] = to_psnr(np.array([r["sse"] for r in rows]).mean(0), location)
        info[name]["region_psnr_yuv_frame_avg"] = np.mean(
            [to_psnr(np.array(r["sse"]), location) for r in rows], axis=0).tolist()
        info[name]["region_mid_amplitude"] = sum(r["mid_cross"] for r in rows)/sum(r["mid_ref2"] for r in rows)
    result = {"scene":scene, "codecs":info, "frames":payload}
    print(tag, info, flush=True)
    (OUT/f"{tag}_details.json").write_text(json.dumps(result, indent=2)+"\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenes", nargs="+", choices=SCENES)
    args = parser.parse_args()
    for scene_tag in args.scenes:
        compare(scene_tag)
