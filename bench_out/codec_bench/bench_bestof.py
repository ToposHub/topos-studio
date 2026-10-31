#!/usr/bin/env python3
"""多轮 best-of 补测：编码轮（全档）与解码轮（全档，乱序）。

产出 JSON 行（stdout 汇总用），与 bench_christmas_1080p.py 同口径：
- 编码计时只累计 codec 调用（产品码控路径 ToposVideoEncoder）；
- 解码为常驻 ToposDecoder + 复用平面，包读取不计入。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path("/Users/testuser/Documents/vscode/topos-color")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bench_out" / "codec_bench"))

import numpy as np
from src.shared.media.metadata import Rational

OUT_DIR = REPO / "bench_out" / "codec_bench"
SRC = Path("/Users/testuser/Desktop/Christmas.mp4")
TIERS = [
    ("proxy", "yuv422p10le"),
    ("lt", "yuv422p10le"),
    ("standard", "yuv422p10le"),
    ("hq", "yuv422p10le"),
    ("4444", "yuv444p10le"),
    ("4444xq", "yuv444p12le"),
]
SLICE_THREADS = min(16, os.cpu_count() or 8)


def enc_round(tag: str):
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from bench_christmas_1080p import src_frames

    for tier, pix_fmt in TIERS:
        out_mov = OUT_DIR / f"christmas_1080p_{tier}.mov"
        cfg = FFmpegEncoderConfig(
            codec="topos", profile=tier, width=1920, height=1080,
            fps=Rational(30000, 1001), pix_fmt=pix_fmt, hwaccel=None)
        enc = ToposVideoEncoder(str(out_mov), cfg, slice_threads=SLICE_THREADS)
        enc.open()
        codec_ns = 0
        n = 0
        orig = enc._encode_packet

        def timed(planes, frame_cfg=None, _orig=orig):
            nonlocal codec_ns, n
            t0 = time.perf_counter_ns()
            r = _orig(planes, frame_cfg)
            codec_ns += time.perf_counter_ns() - t0
            n += 1
            return r

        enc._encode_packet = timed
        for f, _fps in src_frames(pix_fmt):
            enc.encode_frame(f)
        enc.close()
        fps = n / (codec_ns / 1e9)
        print(f"ENC {tag} {tier} {n} {codec_ns/1e9:.3f} {fps:.2f}", flush=True)


def dec_round(tag: str, order):
    from src.shared.codec.topos_binding import ToposCodec, ToposDecoder, ToposMovieFile

    codec = ToposCodec()
    for tier in order:
        movie = ToposMovieFile(codec, str(OUT_DIR / f"christmas_1080p_{tier}.mov"))
        info = movie.info()
        n = int(info.sample_count)
        dec = ToposDecoder(codec, max_slice_workers=SLICE_THREADS)
        h, w = int(info.height), int(info.width)
        geom = ((h, w), (h, (w + 1) // 2), (h, (w + 1) // 2)) \
            if int(info.pixel_format) == 0 else ((h, w),) * 3
        arrays = [np.zeros(g, np.uint16) for g in geom]
        views = dec.make_views(arrays)
        for i in range(min(3, n)):
            b, s = movie.packet_into(i)
            dec.decode_views(bytes(b[:s]), views, 3)
        ts = []
        for i in range(n):
            b, s = movie.packet_into(i)
            pkt = bytes(b[:s])
            t0 = time.perf_counter_ns()
            dec.decode_views(pkt, views, 3)
            ts.append((time.perf_counter_ns() - t0) / 1e6)
        dec.close()
        movie.close()
        fps = n * 1000.0 / statistics.fsum(ts)
        print(f"DEC {tag} {tier} {n} {statistics.fsum(ts)/1000:.3f} {fps:.2f} "
              f"p50={statistics.median(ts):.2f}", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1]
    tag = sys.argv[2]
    if mode == "enc":
        enc_round(tag)
    elif mode == "dec":
        order = sys.argv[3:] or [t for t, _ in TIERS]
        dec_round(tag, order)
