#!/usr/bin/env python3
"""M9 验收基准：V2 canonical VLC vs V1 Rice（native 层，计划 §12.6 门）。

门（spec v2 §6 / 计划 §12.6）：
  G-A 解码提速：V2 native decode 相对 M8 完成后的 V1 ≥1.5×（同机同进程成对实测）
  G-B 码率：V2 相对 V1 不恶化 >1%
  G-C 编码：V2 encoder p95 不退化 >10%
  （重建 bit-exact 与线程一致性由 native 测试钉死，不在本脚本重复）

口径沿用 benchmark_protocol：同源真实素材、同 qp、8 线程、12 帧预热、
每格 3 次重复取 median；解码走 ToposDecoder.decode_views（持久 ctx +
预分配输出平面，产品稳态路径）。
"""
from __future__ import annotations

import ctypes
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

SOURCES = {
    "1080p": (ROOT / "tests/test_videos/2K_prores422HQ.mov", 1920, 1080),
    "4k": (ROOT / "tests/test_videos/4K_prores422HQ.mov", 3840, 2160),
}
THREADS = 8
WARMUP = 12
N = 48          # 每格素材帧数
QPS = (40, 50, 57)
REPEATS = 3
OUT = ROOT / "bench_out/m9"


def source_frames(path: Path, n: int):
    import av

    c = av.open(str(path))
    st = next(s for s in c.streams if s.type == "video")
    st.codec_context.thread_count = 8
    out = []
    for frame in c.decode(st):
        planes = [np.frombuffer(bytes(frame.planes[k]), dtype=np.uint16)
                  .reshape(frame.planes[k].height, frame.planes[k].width).copy()
                  for k in range(3)]
        out.append(planes)
        if len(out) >= n + WARMUP:
            break
    c.close()
    return out[WARMUP:]


def frame_cfg(codec, tb, w: int, h: int, qp: int, em: int):
    fc = tb._CFrameConfig()
    fc.struct_size = ctypes.sizeof(tb._CFrameConfig)
    fc.abi_version = 2
    fc.visible_width, fc.visible_height = w, h
    fc.profile, fc.pixel_format, fc.bit_depth = 3, 0, 10
    fc.qmatrix_id, fc.qp_base = 1, qp
    fc.color_range = fc.color_primaries = fc.color_transfer = fc.color_matrix = 1
    fc.sar_num, fc.sar_den = 1, 1
    fc.reserved[0] = em
    return fc


def main() -> None:
    from src.shared.codec import topos_binding as tb
    from src.shared.codec.topos_binding import (ToposDecoder, ToposCodec,
                                                _CPlaneView)

    codec = ToposCodec()
    codec.set_slice_threads(THREADS)
    ver = codec.version()
    print(f"libtopos_codec {ver.major}.{ver.minor}.{ver.patch} commit={ver.git_commit}")
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"env": {"lib_commit": ver.git_commit, "threads": THREADS,
                       "frames": N, "qps": list(QPS), "repeats": REPEATS},
               "cells": []}

    for res, (path, w, h) in SOURCES.items():
        if not path.exists():
            print(f"[skip] {path} 缺失")
            continue
        frames = source_frames(path, N)
        print(f"[{res}] {len(frames)} 帧 {w}x{h}")
        cw = (w + 1) // 2

        for qp in QPS:
            # —— 编码（两模式；每模式 REPEATS 遍计时）——
            enc = {0: [], 1: []}
            pkts = {0: [], 1: []}
            for rep in range(REPEATS):
                for em in (0, 1):
                    fc = frame_cfg(codec, tb, w, h, qp, em)
                    times = []
                    for planes in frames:
                        t0 = time.perf_counter()
                        pkt, _st = codec.encode_frame(
                            fc, [p.tobytes() for p in planes])
                        times.append((time.perf_counter() - t0) * 1000.0)
                        if rep == 0:
                            pkts[em].append(pkt)
                    enc[em].append(times)
            v1n = np.mean([len(p) for p in pkts[0]])
            v2n = np.mean([len(p) for p in pkts[1]])
            rate_pct = (v2n - v1n) / v1n * 100.0
            e1 = [t for rep in enc[0] for t in rep]
            e2 = [t for rep in enc[1] for t in rep]

            # —— 解码（持久 ctx + 预分配 views；逐包计时，REPEATS 遍）——
            dec = ToposDecoder(codec)
            views = (_CPlaneView * 4)()
            bufs = [
                (ctypes.c_uint16 * (w * h))(),
                (ctypes.c_uint16 * (cw * h))(),
                (ctypes.c_uint16 * (cw * h))(),
            ]
            for p in range(3):
                views[p].struct_size = ctypes.sizeof(_CPlaneView)
                views[p].abi_version = 2
                views[p].pixels = ctypes.cast(bufs[p], ctypes.c_void_p)
                views[p].stride = 0
            dt = {0: [], 1: []}
            for rep in range(REPEATS):
                for em in (0, 1):
                    for pkt in pkts[em]:
                        t0 = time.perf_counter()
                        dec.decode_views(pkt, views, 3)
                        dt[em].append((time.perf_counter() - t0) * 1000.0)
            dec.close()

            # —— 重建 parity 抽验（每模式首帧）——
            f1 = codec.decode(pkts[0][0])
            f2 = codec.decode(pkts[1][0])
            parity = all(a == b for a, b in zip(f1.planes, f2.planes))

            d1_med = statistics.median(dt[0])
            d2_med = statistics.median(dt[1])
            cell = {
                "res": res, "qp": qp,
                "v1_bytes_avg": round(float(v1n), 1),
                "v2_bytes_avg": round(float(v2n), 1),
                "rate_delta_pct": round(rate_pct, 2),
                "enc_v1_ms": {"p50": round(statistics.median(e1), 2),
                              "p95": round(sorted(e1)[int(len(e1) * .95)], 2)},
                "enc_v2_ms": {"p50": round(statistics.median(e2), 2),
                              "p95": round(sorted(e2)[int(len(e2) * .95)], 2)},
                "dec_v1_ms": {"p50": round(d1_med, 3),
                              "p95": round(sorted(dt[0])[int(len(dt[0]) * .95)], 3)},
                "dec_v2_ms": {"p50": round(d2_med, 3),
                              "p95": round(sorted(dt[1])[int(len(dt[1]) * .95)], 3)},
                "decode_speedup": round(d1_med / d2_med, 2),
                "recon_parity": parity,
            }
            results["cells"].append(cell)
            print(f"[{res} qp{qp}] bytes {v1n:.0f}→{v2n:.0f}"
                  f" ({rate_pct:+.2f}%)  dec {d1_med:.2f}→{d2_med:.2f}ms"
                  f" ×{d1_med / d2_med:.2f}  enc p50"
                  f" {cell['enc_v1_ms']['p50']}→{cell['enc_v2_ms']['p50']}ms"
                  f"  parity={parity}")

    (OUT / "vlc_acceptance.json").write_text(json.dumps(results, indent=1))

    # —— 门判定 ——
    print("\n门判定（G-A 提速≥1.5× / G-B 码率≤+1% / G-C enc p95≤+10%）")
    fails = []
    for c in results["cells"]:
        tag = f"{c['res']} qp{c['qp']}"
        ok = [c["decode_speedup"] >= 1.5, c["rate_delta_pct"] <= 1.0,
              c["enc_v2_ms"]["p95"] <= c["enc_v1_ms"]["p95"] * 1.10,
              c["recon_parity"]]
        if not all(ok):
            fails.append((tag, ok))
        print(f"  {tag}: speedup×{c['decode_speedup']}"
              f" rate{c['rate_delta_pct']:+.2f}%"
              f" encp95 {c['enc_v1_ms']['p95']}→{c['enc_v2_ms']['p95']}"
              f" parity={c['recon_parity']} →"
              f" {'PASS' if all(ok) else 'FAIL ' + str(ok)}")
    print("ALL PASS" if not fails else f"FAILED: {fails}")


if __name__ == "__main__":
    main()
