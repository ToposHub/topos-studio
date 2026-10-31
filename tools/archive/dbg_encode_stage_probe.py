#!/usr/bin/env python3
"""编码阶段画像探针（一次性诊断）。

TOPOS_CODEC_PROFILE=1 下经产品路径（ToposVideoEncoder）编码 N 帧，
取 tc_encode_stage_stats 累计与 sized 搜索计数，输出三类闭合账：
  1. 产品墙钟拆分：Python 输入准备（_prepare_planes）/ native 调用 /
     mux 写包 / 其它（预算记账等）；
  2. sized native 墙钟闭合：m7_prepare + 全部 probe + final + 自检
     （应 ≈ native 调用总墙钟；差值即 enc_frame_setup/reserve 等）；
  3. 阶段 CPU 拆分：pad/fill/entropy/crc/copy/asm + M7 的 dct/probe_fill
     （worker 并行累计 CPU，>墙钟×线程数为正常）。
用法：
    TOPOS_CODEC_PROFILE=1 python tools/dbg_encode_stage_probe.py \
        --input file.mov [--frames 16] [--crf 20] [--threads 8]
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


class _CEncStageStats(ctypes.Structure):
    _fields_ = [
        ("pad_ns", ctypes.c_uint64),
        ("fill_ns", ctypes.c_uint64),
        ("entropy_ns", ctypes.c_uint64),
        ("crc_ns", ctypes.c_uint64),
        ("copy_ns", ctypes.c_uint64),
        ("asm_ns", ctypes.c_uint64),
        ("slices", ctypes.c_uint32),
        ("blocks", ctypes.c_uint32),
        ("dct_ns", ctypes.c_uint64),
        ("probe_fill_ns", ctypes.c_uint64),
        ("prep_wall_ns", ctypes.c_uint64),
        ("probe_wall_ns", ctypes.c_uint64),
        ("final_wall_ns", ctypes.c_uint64),
        ("check_wall_ns", ctypes.c_uint64),
    ]


class BenchFrame:
    def __init__(self, planes, width: int, height: int,
                 pixel_format: str = "yuv422p10le"):
        # N09：pixel_format 由加载器按源域传入（444 语料不再伪装 422）
        self.pixel_format = pixel_format
        self.planes = planes
        self.width = width
        self.height = height


def _wrap(obj, attr, acc):
    """实例属性级计时包装：acc[0] += 被调用的墙钟。"""
    orig = getattr(obj, attr)

    def timed(*args, **kwargs):
        t0 = time.perf_counter_ns()
        r = orig(*args, **kwargs)
        acc[0] += time.perf_counter_ns() - t0
        return r

    setattr(obj, attr, timed)


def _load_frames(path: str, n: int):
    """Topos 容器（ToposMovieFile）优先；非 Topos 源（ProRes/DNxHR/H264）
    走 PyAV 解码到源域平面。

    N09（2026-09-27）：历史实现把色度宽度硬编码 w//2（422 域）——4444/
    4444XQ 语料（色度全宽）形状错误。修复：Topos 路径按解码 info 的
    pixel_format 分派（0=4:2:2 半宽色度；1=4:4:4 全宽），alpha（第 4
    平面）恒全宽；PyAV 路径按源 pix_fmt 子采样推导。"""
    from src.shared.codec import topos_binding as tb
    codec = tb.ToposCodec()
    try:
        movie = tb.ToposMovieFile(codec, path)
    except Exception:
        movie = None
    if movie is not None:
        total = movie.info().sample_count
        m = min(n, total)
        decoded = [codec.decode(movie.packet(i % total)) for i in range(m)]
        movie.close()
        info = decoded[0].info
        w, h = info.visible_width, info.visible_height
        cw = w if info.pixel_format == 1 else w // 2
        sub = "444" if info.pixel_format == 1 else "422"
        depth = int(info.bit_depth) if info.bit_depth in (10, 12, 16) else 10
        fmt_name = (f"yuva{sub}p{depth}le" if info.alpha_mode
                    else f"yuv{sub}p{depth}le")
        out = []
        for f in decoded:
            planes = [np.frombuffer(p, dtype=np.uint16) for p in f.planes]
            reshaped = [planes[0].reshape(h, w)]
            for ci in range(1, len(planes)):
                cwid = w if ci == 3 else cw
                reshaped.append(planes[ci].reshape(h, cwid))
            out.append(BenchFrame(reshaped, w, h, fmt_name))
        return out

    import av
    with av.open(path) as cont:
        ctx = cont.streams.video[0].codec_context
        ctx.thread_count = 0
        ctx.thread_type = "FRAME"
        out = []
        for i, frame in enumerate(cont.decode(video=0)):
            if i >= n:
                break
            fp = frame.planes
            h, w = fp[0].height, fp[0].width
            fmt = str(frame.format.name)
            # 子采样推导：444/GBR 家族全宽色度；422/420 半宽；alpha 全宽
            cwid = w if ("444" in fmt or "gbr" in fmt) else w // 2
            widths = [w]
            for ci in range(1, len(fp)):
                widths.append(w if ci == 3 else cwid)
            planes = []
            for p, pw in zip(fp, widths):
                stride = p.line_size // 2
                arr = np.frombuffer(bytes(p), dtype="<u2").reshape(p.height, stride)
                planes.append(np.ascontiguousarray(arr[:, :pw]))
            out.append(BenchFrame(planes, w, h, fmt))
    if not out:
        raise RuntimeError(f"源无视频帧: {path}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--crf", type=int, default=None)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--warmup", type=int, default=4)
    args = ap.parse_args()

    from src.shared.codec.topos_profiles import get_topos_tier
    from src.shared.export.topos_encoder import ToposVideoEncoder

    bf = _load_frames(args.input, args.frames + args.warmup)
    w, h = bf[0].width, bf[0].height
    n = len(bf)

    tier = get_topos_tier("hq")
    cfg = SimpleNamespace(width=w, height=h, fps=Fraction(25, 1),
                          pix_fmt="yuv422p10le", profile="hq", crf=args.crf,
                          has_alpha=False)
    out = Path("bench_out") / f"dbg_enc_probe_{w}x{h}_{'qp'+str(args.crf) if args.crf else 'rate'}.mov"
    out.parent.mkdir(exist_ok=True)
    enc = ToposVideoEncoder(str(out), cfg, slice_threads=args.threads)
    enc.open()
    lib = enc._codec._lib
    lib.tc_dev_encode_stats_reset.argtypes = []
    lib.tc_dev_encode_stats_get.argtypes = [ctypes.POINTER(_CEncStageStats)]
    lib.tc_dev_sized_reset.argtypes = []
    lib.tc_dev_sized_iters.restype = ctypes.c_uint64
    lib.tc_dev_sized_calls.restype = ctypes.c_uint64
    for i in range(args.warmup):
        enc.encode_frame(bf[i])

    # 计时挂点（warmup 后：mux 已建立、几何已定）
    prep_ns = [0]
    native_ns = [0]
    mux_ns = [0]
    _wrap(enc, "_prepare_planes", prep_ns)
    _wrap(enc._codec, "encode_sized", native_ns)
    _wrap(enc._codec, "encode_frame", native_ns)
    _wrap(enc._mux, "add_packet", mux_ns)

    lib.tc_dev_encode_stats_reset()
    lib.tc_dev_sized_reset()
    t0 = time.perf_counter()
    for i in range(args.warmup, n):
        enc.encode_frame(bf[i])
    wall = time.perf_counter() - t0
    iters = lib.tc_dev_sized_iters()
    calls = lib.tc_dev_sized_calls()
    enc.close()

    m = n - args.warmup
    st = _CEncStageStats()
    lib.tc_dev_encode_stats_get(ctypes.byref(st))
    ms = lambda ns: ns / 1e6 / m
    fill, ent, crc = ms(st.fill_ns), ms(st.entropy_ns), ms(st.crc_ns)
    pad, copy, asm = ms(st.pad_ns), ms(st.copy_ns), ms(st.asm_ns)
    dct, pfill = ms(st.dct_ns), ms(st.probe_fill_ns)
    prep_w, probe_w = ms(st.prep_wall_ns), ms(st.probe_wall_ns)
    final_w, check_w = ms(st.final_wall_ns), ms(st.check_wall_ns)
    serial = pad + copy + asm
    cpu = fill + ent + crc + serial + dct + pfill

    wall_ms = wall / m * 1000
    tag = 'qp' + str(args.crf) if args.crf else 'rate(sized)'
    print(f"== {w}x{h} {tag} t{args.threads}，{m} 帧 ==")
    print(f"  wall avg {wall_ms:.1f} ms  ({m / wall:.2f} fps)")
    print(f"  产品拆分: python输入 {prep_ns[0]/1e6/m:.1f}  native {native_ns[0]/1e6/m:.1f}  "
          f"mux {mux_ns[0]/1e6/m:.2f}  其它 {wall_ms - (prep_ns[0]+native_ns[0]+mux_ns[0])/1e6/m:.1f} ms")
    print(f"  native闭合: prep {prep_w:.1f} + probe {probe_w:.1f} + final {final_w:.1f} + "
          f"自检 {check_w:.1f} = {prep_w+probe_w+final_w+check_w:.1f} ms")
    print(f"  CPU/帧: fill {fill:.1f}  probe量化 {pfill:.1f}  DCT {dct:.1f}  entropy {ent:.1f}  "
          f"crc {crc:.2f}  | 串行: pad {pad:.2f} copy {copy:.2f} asm {asm:.2f}  合计 {cpu:.1f} ms")
    if cpu:
        print(f"  占比: probe量化 {pfill/cpu*100:.0f}%  fill {fill/cpu*100:.0f}%  DCT {dct/cpu*100:.0f}%  "
              f"entropy {ent/cpu*100:.0f}%  crc {crc/cpu*100:.0f}%  串行 {serial/cpu*100:.0f}%")
    if calls:
        print(f"  sized: probe 编码 {iters / calls:.2f} 次/帧（calls {calls}）")
    print(f"  有效并行: {cpu / wall_ms:.1f}x（CPU 合计/产品墙钟）")


if __name__ == "__main__":
    main()
