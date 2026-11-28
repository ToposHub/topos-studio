#!/usr/bin/env python3
"""Locate a user-supplied screenshot against the source test videos."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from measure import OUT, ROOT, SOURCES, source_path

W, H = 192, 108
SCREEN_CROP = np.s_[8:-8, 14:-14]
SAMPLE_FPS = 5


def feature(image: np.ndarray) -> list[np.ndarray]:
    lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.float32)
    y = cv2.GaussianBlur(lab[:, :, 0], (5, 5), 0.9)
    gray = y[SCREEN_CROP]
    gray = (gray - gray.mean()) / (gray.std() + 0.01)
    g = cv2.Laplacian(y, cv2.CV_32F)[SCREEN_CROP]
    g = g / (g.std() + 0.01)
    ab = lab[:, :, 1:][SCREEN_CROP]
    ab -= ab.mean(axis=(0, 1))
    ab /= ab.std(axis=(0, 1)) + 0.01
    return [gray, g, ab]


def compare(path: Path, screenshot: list[np.ndarray]) -> list[dict]:
    process = subprocess.Popen([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-filter_threads", "1",
        "-threads", "4", "-i", str(path), "-map", "0:v:0", "-an",
        "-vf", f"fps=fps={SAMPLE_FPS}:round=near,scale={W}:{H}:flags=area,format=rgb24",
        "-pix_fmt", "rgb24", "-c:v", "rawvideo", "-threads", "1", "-f", "rawvideo", "-",
    ], stdout=subprocess.PIPE)
    best = []
    n = 0
    assert process.stdout
    while buf := process.stdout.read(W * H * 3):
        assert len(buf) == W * H * 3
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 3)
        vector = feature(frame)
        sims = [float(np.mean(a*b)) for a, b in zip(vector,screenshot,strict=True)]
        score = 0.70*sims[0] + 0.15*sims[1] + 0.15*sims[2]
        best.append({"path": str(path), "seconds": n/SAMPLE_FPS,
                     "score": score, "similarities": sims,
                     "frame": np.copy(frame)})
        n += 1
    assert process.wait() == 0, path
    ordered = sorted(best,key=lambda x:x['score'],reverse=True)
    windows = []
    for row in ordered:
        if all(abs(row['seconds']-prev['seconds'])>0.8 for prev in windows):
            windows.append(row)
        if len(windows)==6:
            break
    print(path.name, n, [(round(x['seconds'],2),round(x['score'],3)) for x in windows], flush=True)
    return windows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("screenshot",type=Path)
    parser.add_argument("--other",action="store_true")
    args=parser.parse_args()
    thumb=Image.open(args.screenshot).convert("RGB").resize((W,H),Image.Resampling.LANCZOS)
    target=feature(np.array(thumb))
    paths=[source_path(clip) for clip in SOURCES]
    if args.other:
        parent=ROOT/'tests/test_videos/达芬奇转码_测速_2026-11-27/H264_H265_全长/4K/H265'
        paths=[p for p in sorted(parent.glob('*.mov')) if p.name not in {s.name for s in paths}]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        groups=list(pool.map(lambda p:compare(p,target),paths))
    results=sorted([r for g in groups for r in g], key=lambda r:r['score'],reverse=True)[:12]
    panel=Image.new("RGB",(984,492),"#121820")
    draw=ImageDraw.Draw(panel)
    font=ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf",14)
    panel.paste(thumb,(4,4))
    draw.text((4,116),"Screenshot",font=font,fill="white")
    for i,r in enumerate(results,1):
        x=(i%5)*196+4
        y=(i//5)*156+4
        panel.paste(Image.fromarray(r.pop('frame')),(x,y))
        draw.text((x,y+111),f"{r['seconds']:.1f} s | score {r['score']:.3f}",
                  font=font,fill="white")
        draw.text((x,y+130),Path(r['path']).parent.name[:23],font=font,fill="white")
    tag="search_other" if args.other else "search_dnxhr"
    panel.save(OUT/f'{tag}.png')
    (OUT/f'{tag}.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+"\n")
