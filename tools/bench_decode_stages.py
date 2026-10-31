#!/usr/bin/env python3
"""解码阶段画像探针（解码深化批次基线/回归用）。

口径：native 路径（packet 预取 + 输出平面预分配 + tc_frame_decode 直调），
TOPOS_CODEC_PROFILE=1 下逐帧取 tc_decode_stage_stats 累计，输出每帧墙钟与
阶段 CPU 拆分（scan/crc/entropy/dequant+idct）。熵与 IDCT 为 worker 并行累计
CPU 时间（>墙钟×线程数为正常）。

V1/V2 的 dequant+idct 为每 slice 1/64 确定性抽样 × 总块数归一的估计值
（逐块取时钟曾使带探针 wall 比无探针慢 ~25%，阶段绝对值失真；抽样后
开/关探针 wall 差 ≤3%）。V3 intra 另对同一抽样块分别计熵读取与预测、
反量化、逆变换和写回，再以 slice 墙钟闭合未计入的小项。阶段占比是诊断
口径，不能直接推导整条播放链耗时；正式速度测试关闭 profile。

用法：
    TOPOS_CODEC_PROFILE=1 .venv/bin/python tools/bench_decode_stages.py \
        [--frames 60] [--videos 2K 4K]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def bench(path: str, frames: int) -> None:
    from src.shared.codec import topos_binding as tb

    codec = tb.ToposCodec()
    lib = codec._lib
    lib.tc_dev_decode_stats_reset.argtypes = []
    lib.tc_dev_decode_stats_get.argtypes = [ctypes.POINTER(tb._CDecodeStageStats)]
    lib.tc_dev_decode_stats_reset.restype = None

    movie = tb.ToposMovieFile(codec, path)
    total = movie.info().sample_count
    n = min(frames, total)
    packets = [movie.packet(i % total) for i in range(12 + n)]

    info = tb._CFrameOutput()
    buf0 = ctypes.create_string_buffer(packets[0], max(len(packets[0]), 1))
    rc = lib.tc_frame_decode(buf0, len(packets[0]), None, None, ctypes.byref(info))
    assert rc in (0, 1), f"query rc={rc}"
    ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
    for p in range(info.plane_count):
        w, h = codec.plane_geometry(info, p)
        arr = (ctypes.c_uint16 * (w * h))()
        ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))
    bufs = [ctypes.create_string_buffer(p, max(len(p), 1)) for p in packets]

    for i in range(12):  # 预热（含 LUT 构建等一次性成本）
        lib.tc_frame_decode(bufs[i], len(packets[i]), ptrs, None, ctypes.byref(info))

    st = tb._CDecodeStageStats()
    times = []
    lib.tc_dev_decode_stats_reset()
    for i in range(12, 12 + n):
        t0 = time.perf_counter()
        rc = lib.tc_frame_decode(bufs[i], len(packets[i]), ptrs, None,
                                 ctypes.byref(info))
        times.append((time.perf_counter() - t0) * 1000.0)
        assert rc in (0, 1), f"decode rc={rc}"
    lib.tc_dev_decode_stats_get(ctypes.byref(st))
    movie.close()

    avg = statistics.fmean(times)
    f = n
    print(f"== {Path(path).name}（{info.visible_width}x{info.visible_height}，{n} 帧）==")
    print(f"  wall avg {avg:.2f} ms  ({1000.0 / avg:.1f} fps)   "
          f"p50 {statistics.median(times):.2f}  "
          f"p95 {sorted(times)[int(0.95 * (n - 1))]:.2f} ms")
    if st.blocks == 0 and st.entropy_ns == 0:
        print("  !! 阶段统计为 0 —— 需要 TOPOS_CODEC_PROFILE=1 启动进程")
        return
    ent = st.entropy_ns / 1e6 / f
    dq = st.dequant_idct_ns / 1e6 / f
    crc = st.crc_ns / 1e6 / f
    scan = st.scan_ns / 1e6 / f
    cpu = ent + dq + crc + scan
    print(f"  CPU/帧: entropy {ent:.2f} ({cpu and ent / cpu * 100:.0f}%)  "
          f"dequant+idct {dq:.2f} ({cpu and dq / cpu * 100:.0f}%)  "
          f"crc {crc:.3f}  scan {scan:.3f}  | 合计 {cpu:.2f} ms")
    print(f"  blocks {st.blocks / f:.0f}/帧  dc-only {st.dc_only_blocks / f:.0f}"
          f"（{st.blocks and st.dc_only_blocks / st.blocks * 100:.1f}%）  "
          f"非零 AC {st.nonzero_ac / f:.0f}/帧")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--videos", nargs="+", default=["2K", "4K"],
                    help="2K/4K 关键字或完整路径")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许工作树有未提交改动；结果仅作历史观察")
    args = ap.parse_args()
    from tools.bench_manifest import ManifestMismatch, build_manifest
    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        ap.error(f"benchmark manifest mismatch: {exc}")
    print("manifest: " + str({k: manifest[k] for k in (
        "topos_version", "dylib_commit_embedded", "dylib_sha256",
        "git_head", "git_dirty", "slice_threads")
        if k in manifest}))
    paths = []
    for v in args.videos:
        if "/" in v:
            paths.append(v)
        else:
            # F01（2026-09-27）：默认视频走冻结语料规范名
            paths.append(str(PROJECT_ROOT / f"tests/test_videos/{v}_topos_422.mov"))
    for p in paths:
        if Path(p).exists():
            bench(p, args.frames)
        else:
            print(f"跳过（不存在）: {p}")


if __name__ == "__main__":
    main()
