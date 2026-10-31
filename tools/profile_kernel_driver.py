#!/usr/bin/env python3
"""内核剖析驱动：直调 tc_frame_encode / tc_frame_decode 的纯内核循环。

用法（配合 macOS `sample` 或其他采样器）：
    .venv/bin/python tools/profile_kernel_driver.py --mode encode --res 4k \
        --seconds 15 --threads 8 --qp 20 &
    sample <pid> 10 -file bench_out/profile_enc.txt

口径：不含 mux / Python 输入校验，只测 codec 内核（含 pad/DCT/量化/熵/位写）。
"""
from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.shared.codec import topos_binding as tb  # noqa: E402

SOURCES = {
    "2k": ("tests/test_videos/2K_prores422HQ.mov", 1920, 1080),
    "4k": ("tests/test_videos/4K_prores422HQ.mov", 3840, 2160),
}


def load_frames(path: str, w: int, h: int, n: int):
    import av

    frames = []
    with av.open(PROJECT_ROOT / path) as cont:
        ctx = cont.streams.video[0].codec_context
        ctx.thread_count = 0
        ctx.thread_type = "FRAME"
        for i, frame in enumerate(cont.decode(video=0)):
            if i >= n:
                break
            planes = []
            for p, pw in zip(frame.planes, (w, w // 2, w // 2)):
                raw = bytes(p)
                stride = p.line_size // 2
                arr = np.frombuffer(raw, dtype="<u2").reshape(p.height, stride)
                planes.append(np.ascontiguousarray(arr[:, :pw]))
            frames.append(tuple(planes))
    if len(frames) < n:
        raise RuntimeError(f"源帧不足：请求 {n}，实得 {len(frames)}")
    return frames


def make_config(w: int, h: int, qp: int) -> tb._CFrameConfig:
    cfg = tb._CFrameConfig()
    cfg.struct_size = ctypes.sizeof(tb._CFrameConfig)
    cfg.abi_version = tb.TOPOS_CODEC_ABI_VERSION
    cfg.visible_width = w
    cfg.visible_height = h
    cfg.profile = 3  # Standard/HQ 预设族
    cfg.pixel_format = 0  # 0=4:2:2
    cfg.bit_depth = 10
    cfg.qmatrix_id = 1
    cfg.qp_base = qp
    cfg.slice_rows = 32
    cfg.color_range = 1
    cfg.color_primaries = 1
    cfg.color_transfer = 1
    cfg.color_matrix = 1
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("encode", "decode"), required=True)
    ap.add_argument("--res", choices=list(SOURCES), default="4k")
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--qp", type=int, default=20)
    args = ap.parse_args()

    src, w, h = SOURCES[args.res]
    print(f"[load] {src} …", flush=True)
    frames = load_frames(src, w, h, args.frames)

    codec = tb.ToposCodec()
    codec.set_slice_threads(args.threads)
    lib = codec._lib

    cfg = make_config(w, h, args.qp)
    cap = lib.tc_frame_packet_bound(ctypes.byref(cfg))
    pkt = (ctypes.c_uint8 * cap)()
    stats = tb._CFrameStats()
    stats.struct_size = ctypes.sizeof(tb._CFrameStats)
    stats.abi_version = tb.TOPOS_CODEC_ABI_VERSION

    inputs = []
    for fr in frames:
        cin = tb._CFrameInput()
        cin.struct_size = ctypes.sizeof(tb._CFrameInput)
        cin.abi_version = tb.TOPOS_CODEC_ABI_VERSION
        keep = []
        for p in range(3):
            arr = np.ascontiguousarray(fr[p])
            keep.append(arr)
            cin.planes[p] = arr.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16))
            cin.strides[p] = arr.shape[1]
        inputs.append((cin, keep))

    if args.mode == "encode":
        rc0 = lib.tc_frame_encode(ctypes.byref(cfg), ctypes.byref(inputs[0][0]),
                                  pkt, cap, ctypes.byref(stats))
        if rc0 != 0:
            raise RuntimeError(f"encode rc={rc0}")

        def one(i: int) -> None:
            lib.tc_frame_encode(ctypes.byref(cfg), ctypes.byref(inputs[i][0]),
                                pkt, cap, ctypes.byref(stats))
    else:
        rc0 = lib.tc_frame_encode(ctypes.byref(cfg), ctypes.byref(inputs[0][0]),
                                  pkt, cap, ctypes.byref(stats))
        if rc0 != 0:
            raise RuntimeError(f"encode rc={rc0}")
        size0 = stats.packet_size
        info = tb._CFrameOutput()
        lib.tc_frame_decode(pkt, size0, None, None, ctypes.byref(info))
        ptrs = (ctypes.POINTER(ctypes.c_uint16) * tb.TC_FRAME_MAX_PLANES)()
        views = []
        for p in range(int(info.plane_count)):
            pw, ph = codec.plane_geometry(info, p)
            arr = (ctypes.c_uint16 * (pw * ph))()
            views.append(arr)
            ptrs[p] = ctypes.cast(arr, ctypes.POINTER(ctypes.c_uint16))

        def one(i: int) -> None:
            rc = lib.tc_frame_decode(pkt, size0, ptrs, None, ctypes.byref(info))
            if rc not in (0, 1):
                raise RuntimeError(f"decode rc={rc}")

    # 预热 + 计时循环
    n = len(inputs)
    for i in range(n):
        one(i)
    print(f"[run] {args.mode} {args.res} t{args.threads} qp{args.qp} "
          f"{args.seconds}s …", flush=True)
    runs = []
    t_end = time.perf_counter() + args.seconds
    count = 0
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        for i in range(n):
            one(i)
        runs.append(n / (time.perf_counter() - t0))
        count += n
    fps = statistics.median(runs)
    print(f"[done] {args.mode} {args.res}: {fps:.2f} fps "
          f"({count} 帧内核循环)", flush=True)


if __name__ == "__main__":
    main()
