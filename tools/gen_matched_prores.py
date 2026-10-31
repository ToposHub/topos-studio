#!/usr/bin/env python3
"""同输入 ProRes 对照生成：母版 → 2K/4K 目标尺寸 → Apple ProRes 编码。

用途：把 "TOPOS 高频能量低于 ProRes" 的观察从**缩放器差异**中剥离。
``tests/test_videos/2K_prores422Proxy.mov`` 等基准是 Apple 工具链用**自己的
缩放器**从 4K 母版生成的；若该缩放器比 ffmpeg 的 bicubic 保留更多细节，
则"ProRes 更锐"是语料构造副作用而非编码器差异。本工具用**与 TOPOS 编码
完全相同的 ffmpeg 缩放管线**（``scale=in_range=tv:out_range=tv``，默认
bicubic）生成 ProRes，两侧因此吃进逐位相同的输入像素。

用法::

    .venv/bin/python tools/gen_matched_prores.py --size 2K --profile 0 --tier proxy
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MEDIA = REPO / "tests" / "test_videos"
MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR = REPO / "bench_out" / "qgain"
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}
PROFILES = {"proxy": 0, "lt": 1, "standard": 2, "hq": 3}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="2K", choices=list(SIZES))
    ap.add_argument("--tier", action="append", default=["proxy"],
                    choices=list(PROFILES))
    ap.add_argument("--encoder", default="prores_videotoolbox",
                    choices=["prores_videotoolbox", "prores_ks"])
    ap.add_argument("--qscale", type=int, default=None,
                    help="prores_ks 专用：量化标度（越大越省码率）")
    ap.add_argument("--frames", type=int, default=None,
                    help="只编码前 N 帧（试码率用）")
    ap.add_argument("--tag", default=None, help="输出文件名前缀覆盖")
    args = ap.parse_args()

    w, h = SIZES[args.size]
    ff = "ffmpeg"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for tier in args.tier:
        tag = args.tag or f"mprores_{args.size}_{tier}"
        out = OUT_DIR / f"{tag}.mov"
        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(MASTER),
            "-vf", "scale=in_range=tv:out_range=tv",
            "-c:v", args.encoder, "-profile:v", str(PROFILES[tier]),
        ]
        if args.encoder == "prores_videotoolbox":
            cmd += ["-allow_sw", "1", "-pix_fmt", "p210le"]
        else:
            cmd += ["-pix_fmt", "yuv422p10le"]
        if args.qscale is not None:
            cmd += ["-qscale:v", str(args.qscale)]
        if args.frames is not None:
            cmd += ["-frames:v", str(args.frames)]
        cmd += [
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-colorspace", "bt709", "-color_range", "tv",
            "-an",
            str(out),
        ]
        subprocess.run(cmd, check=True)
        n = args.frames or 487
        print(f"{out.name}: {out.stat().st_size / 1e6:.1f} MB "
              f"({w}x{h}, profile {PROFILES[tier]}, {n} 帧 → "
              f"{out.stat().st_size * 8 / (n / 25) / 1e6:.1f} Mbps)",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
