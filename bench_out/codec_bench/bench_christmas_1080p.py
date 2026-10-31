#!/usr/bin/env python3
"""Christmas.mp4 → Topos 六档 1080P 编码/解码速率基准（本机 Apple M1）。

口径对齐 docs/codec/topos_codec_speed_matrix_2026-09-15.md：
- 编码走产品导出路径（ToposVideoEncoder：首帧 sized 搜索 + 帧间反馈
  单遍码控，entropy=rans2 产品默认），计时只累计 codec 调用
  （_encode_packet），源 H.264 解码/像素格式转换/MOV 封装不计入；
- 解码用常驻 ToposDecoder + 复用输出平面（decode_views），包从
  ToposMovieFile 逐帧读出（读取/物化 bytes 不计入计时）；
- 码率 = 编码产出 MOV 文件大小 × 8 / 时长（含容器开销，真实落盘）。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np

from src.shared.media.metadata import Rational

REPO = Path("/Users/testuser/Documents/vscode/topos-color")
sys.path.insert(0, str(REPO))

SRC = Path("/Users/testuser/Desktop/Christmas.mp4")
OUT_DIR = REPO / "bench_out" / "codec_bench"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 六档 → 导出 pix_fmt（tier 声明的默认色彩结构/位深）
TIERS = [
    ("proxy", "yuv422p10le"),
    ("lt", "yuv422p10le"),
    ("standard", "yuv422p10le"),
    ("hq", "yuv422p10le"),
    ("4444", "yuv444p10le"),
    ("4444xq", "yuv444p12le"),
]

SLICE_THREADS = min(16, os.cpu_count() or 8)  # 产品前台导出线程策略

# 无风扇 M1 Air：持续负载降频 35-65%（实测复验）。档前/解码前冷却，
# 让各档在相近热状态下测量；绝对值仍代表"非持续满载"的典型工况。
COOLDOWN_S = int(os.environ.get("BENCH_COOLDOWN", "30"))


def src_frames(pix_fmt: str):
    """流式解码源 H.264 → tier pix_fmt 的 uint16 平面三元组。"""
    container = av.open(str(SRC))
    stream = container.streams.video[0]
    fps = stream.average_rate  # 30000/1001
    for frame in container.decode(video=0):
        rf = frame.reformat(format=pix_fmt)
        planes = []
        for plane in rf.planes:
            buf = bytes(plane)
            ls = plane.line_size
            h = plane.height
            arr = np.frombuffer(buf, dtype=np.uint8, count=ls * h)
            arr = arr.reshape(h, ls)
            w2 = ls // 2
            planes.append(np.ascontiguousarray(arr[:, : w2 * 2].view("<u2")))
        f = SimpleNamespace(
            pixel_format=pix_fmt, planes=tuple(planes),
            width=rf.width, height=rf.height,
        )
        yield f, fps
    container.close()


def bench_tier(tier: str, pix_fmt: str) -> dict:
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.codec.topos_binding import ToposCodec, ToposDecoder, ToposMovieFile

    out_mov = OUT_DIR / f"christmas_1080p_{tier}.mov"
    print(f"\n=== tier={tier} pix_fmt={pix_fmt} ===", flush=True)
    print(f"  cooldown {COOLDOWN_S}s...", flush=True)
    time.sleep(COOLDOWN_S)

    cfg = FFmpegEncoderConfig(
        codec="topos", profile=tier,
        width=1920, height=1080,
        fps=Rational(30000, 1001),
        pix_fmt=pix_fmt,
        hwaccel=None,
    )
    enc = ToposVideoEncoder(str(out_mov), cfg, slice_threads=SLICE_THREADS)
    enc.open()

    # 计时包装：只累计 codec 调用（产品码控：首帧 sized + 反馈单遍）
    codec_ns = 0
    n_calls = 0
    qps = []
    frame_bytes = []
    orig = enc._encode_packet

    def timed(planes, frame_cfg=None):
        nonlocal codec_ns, n_calls
        t0 = time.perf_counter_ns()
        pkt, st = orig(planes, frame_cfg)
        codec_ns += time.perf_counter_ns() - t0
        n_calls += 1
        qps.append(int(st.qp_base))
        frame_bytes.append(len(pkt))
        return pkt, st

    enc._encode_packet = timed

    t_wall0 = time.perf_counter()
    n_frames = 0
    src_fps = None
    for f, fps in src_frames(pix_fmt):
        if src_fps is None:
            src_fps = float(fps)
        enc.encode_frame(f)
        n_frames += 1
        if n_frames % 300 == 0:
            print(f"  encoded {n_frames} frames...", flush=True)
    enc.close()
    wall = time.perf_counter() - t_wall0

    assert n_calls == n_frames, (n_calls, n_frames)
    enc_fps = n_frames / (codec_ns / 1e9)
    duration = n_frames / src_fps
    size = out_mov.stat().st_size
    mbps = size * 8.0 / duration / 1e6
    mean_qp = statistics.mean(qps)

    print(f"  encode: {n_frames}f codec={codec_ns/1e9:.2f}s → {enc_fps:.1f} fps"
          f" | wall={wall:.1f}s | {size/1e6:.1f} MB → {mbps:.1f} Mbps | qp~{mean_qp:.1f}",
          flush=True)

    # —— 解码：常驻 decoder + 复用平面；包逐帧读（读取不计入计时）——
    print(f"  cooldown {COOLDOWN_S}s before decode...", flush=True)
    time.sleep(COOLDOWN_S)
    codec = ToposCodec()
    movie = ToposMovieFile(codec, str(out_mov))
    info = movie.info()
    n = int(info.sample_count)
    dec = ToposDecoder(codec, max_slice_workers=SLICE_THREADS)
    h, w = int(info.height), int(info.width)
    if int(info.pixel_format) == 0:  # 4:2:2
        geom = ((h, w), (h, (w + 1) // 2), (h, (w + 1) // 2))
    else:                            # 4:4:4 / GBR
        geom = ((h, w), (h, w), (h, w))
    arrays = [np.zeros(g, np.uint16) for g in geom]
    views = dec.make_views(arrays)

    # 预热 3 帧（线程池/懒表初始化）
    for i in range(min(3, n)):
        buf, sz = movie.packet_into(i)
        dec.decode_views(bytes(buf[:sz]), views, len(arrays))

    dec_ns = 0
    per_ms = []
    for i in range(n):
        buf, sz = movie.packet_into(i)  # 读取/物化不计入
        pkt = bytes(buf[:sz])
        t0 = time.perf_counter_ns()
        dec.decode_views(pkt, views, len(arrays))
        dt = time.perf_counter_ns() - t0
        dec_ns += dt
        per_ms.append(dt / 1e6)
    dec.close()
    movie.close()

    dec_fps = n / (dec_ns / 1e9)
    per_sorted = sorted(per_ms)
    p50 = per_sorted[len(per_sorted) // 2]
    p95 = per_sorted[min(len(per_sorted) - 1, int(len(per_sorted) * 0.95))]
    print(f"  decode: {n}f codec={dec_ns/1e9:.2f}s → {dec_fps:.1f} fps"
          f" | p50={p50:.2f}ms p95={p95:.2f}ms", flush=True)

    return {
        "tier": tier, "pix_fmt": pix_fmt,
        "frames": n_frames, "src_fps": src_fps, "duration_s": duration,
        "encode": {
            "fps": enc_fps, "codec_s": codec_ns / 1e9,
            "wall_s": wall, "mean_qp": mean_qp,
            "mean_payload_bytes": statistics.mean(frame_bytes),
        },
        "decode": {
            "fps": dec_fps, "codec_s": dec_ns / 1e9,
            "avg_ms": statistics.mean(per_ms), "p50_ms": p50, "p95_ms": p95,
        },
        "file": {
            "path": str(out_mov), "bytes": size,
            "mbps_incl_container": mbps,
            "payload_mbps": statistics.mean(frame_bytes) * 8.0 * src_fps / 1e6,
        },
    }


def main() -> int:
    results = {
        "schema": "topos.christmas_1080p_bench.v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "source": str(SRC),
        "slice_threads": SLICE_THREADS,
        "host": {
            "machine": "Apple M1 MacBook Air (8C, 8GB)",
            "platform": f"{sys.platform} {os.uname().machine}",
        },
        "tiers": [],
    }
    for tier, pix_fmt in TIERS:
        results["tiers"].append(bench_tier(tier, pix_fmt))

    out_json = OUT_DIR / f"christmas_1080p_results_{datetime.now():%Y%m%d}.json"
    out_json.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"\nresults → {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
