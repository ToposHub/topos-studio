#!/usr/bin/env python3
"""导出 packed 路径分层探针（真实时间线导出口径）。

与 dbg_encode_stage_probe.py（planar 直通，源再编码）互补：本探针把 Topos
源解码帧离线转成 BGR packed（模拟时间线渲染输出 bgr24/bgr48le），经产品
路径 ToposVideoEncoder.encode_frame → _prepare_packed → _try_native_packed
（tc_convert_packed_rgb 融合 RGB→YUV）→ native 编码 → mux，成对对比两库。

暴露的段：
  convert   = _prepare_planes（native 融合 RGB→YUV；旧库无 AVX2 cvt 8fba2bdd）
  native    = encode_sized/encode_frame（DCT/量化/熵；56a81498/44e35497/bab66eaa）
  mux       = add_packet
  其它      = 预算记账等

用法：
    TOPOS_CODEC_PROFILE=1 TOPOS_CODEC_LIB=<dylib> \
      python tools/dbg_export_packed_probe.py --input 4K_topos.mov \
      [--frames 16] [--threads 16] [--depth 8|10]
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


class PackedFrame:
    """ToposVideoEncoder.encode_frame 的 packed duck-type 输入（无平面）。"""

    def __init__(self, data: np.ndarray, pixfmt: str, w: int, h: int) -> None:
        self.data = data
        self.pixel_format = pixfmt
        self.width = w
        self.height = h
        self.planes = ()
        self.is_planar = False


def _yuv422p10_to_bgr(y: np.ndarray, u: np.ndarray, v: np.ndarray,
                      depth: int) -> np.ndarray:
    """yuv422p10le 平面 → BGR packed（离线、不计时；保内容密度即可）。

    bt709 limited-range 反矩阵；10-bit 码值 → [0,1] → 8/16-bit BGR。
    """
    h, w = y.shape
    uf = np.repeat(u, 2, axis=1)[:, :w].astype(np.float32)
    vf = np.repeat(v, 2, axis=1)[:, :w].astype(np.float32)
    yf = y.astype(np.float32)
    yn = (yf - 64.0) / 876.0
    un = (uf - 512.0) / 896.0
    vn = (vf - 512.0) / 896.0
    r = yn + 1.5748 * vn
    g = yn - 0.1873 * un - 0.4681 * vn
    b = yn + 1.8556 * un
    bgr = np.stack([b, g, r], axis=-1)  # BGR 序
    np.clip(bgr, 0.0, 1.0, out=bgr)
    if depth == 8:
        return (bgr * 255.0 + 0.5).astype(np.uint8)
    return (bgr * 65535.0 + 0.5).astype(np.uint16)


def _load_packed_frames(path: str, n: int, depth: int):
    from src.shared.codec import topos_binding as tb
    codec = tb.ToposCodec()
    movie = tb.ToposMovieFile(codec, path)
    total = movie.info().sample_count
    m = min(n, total)
    decoded = [codec.decode(movie.packet(i % total)) for i in range(m)]
    movie.close()
    w = decoded[0].info.visible_width
    h = decoded[0].info.visible_height
    pixfmt = 'bgr24' if depth == 8 else 'bgr48le'
    out = []
    for f in decoded:
        planes = [np.frombuffer(p, dtype=np.uint16) for p in f.planes]
        y = planes[0].reshape(h, w)
        u = planes[1].reshape(h, w // 2)
        v = planes[2].reshape(h, w // 2)
        bgr = _yuv422p10_to_bgr(y, u, v, depth)
        out.append(PackedFrame(np.ascontiguousarray(bgr), pixfmt, w, h))
    if not out:
        raise RuntimeError(f"源无视频帧: {path}")
    return out


def _wrap(obj, attr, acc):
    orig = getattr(obj, attr)

    def timed(*args, **kwargs):
        t0 = time.perf_counter_ns()
        r = orig(*args, **kwargs)
        acc[0] += time.perf_counter_ns() - t0
        return r

    setattr(obj, attr, timed)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--depth", type=int, default=8, choices=(8, 10))
    ap.add_argument("--warmup", type=int, default=4)
    ap.add_argument("--no-fused", action="store_true",
                    help="禁用 native 融合转换（走 numpy 参考路径对照）")
    args = ap.parse_args()

    if args.no_fused:
        import os
        os.environ['TOPOS_ENCODER_DISABLE_FUSED_CVT'] = '1'

    from src.shared.codec.topos_profiles import get_topos_tier  # noqa: F401
    from src.shared.export.topos_encoder import ToposVideoEncoder

    bf = _load_packed_frames(args.input, args.frames + args.warmup, args.depth)
    w, h = bf[0].width, bf[0].height
    n = len(bf)

    cfg = SimpleNamespace(width=w, height=h, fps=Fraction(25, 1),
                          pix_fmt="yuv422p10le", profile="hq", crf=None,
                          has_alpha=False)
    tag_in = 'bgr24' if args.depth == 8 else 'bgr48le'
    out = Path("bench_out") / f"dbg_packed_{w}x{h}_{tag_in}_t{args.threads}.mov"
    out.parent.mkdir(exist_ok=True)
    enc = ToposVideoEncoder(str(out), cfg, slice_threads=args.threads)
    enc.open()

    for i in range(args.warmup):
        enc.encode_frame(bf[i])

    prep_ns = [0]
    native_ns = [0]
    mux_ns = [0]
    _wrap(enc, "_prepare_planes", prep_ns)
    _wrap(enc._codec, "encode_sized", native_ns)
    _wrap(enc._codec, "encode_frame", native_ns)
    _wrap(enc._mux, "add_packet", mux_ns)

    fused = getattr(enc, "_cvt_available", None)
    t0 = time.perf_counter()
    for i in range(args.warmup, n):
        enc.encode_frame(bf[i])
    wall = time.perf_counter() - t0
    enc.close()

    m = n - args.warmup
    wall_ms = wall / m * 1000
    prep = prep_ns[0] / 1e6 / m
    nat = native_ns[0] / 1e6 / m
    mux = mux_ns[0] / 1e6 / m
    other = wall_ms - prep - nat - mux
    mode = "numpy" if args.no_fused else f"native-fused(avail={fused})"
    print(f"== PACKED {w}x{h} {tag_in} → yuv422p10le hq t{args.threads}，{m} 帧 [{mode}] ==")
    print(f"  wall avg {wall_ms:.1f} ms  ({m / wall:.2f} fps)")
    print(f"  拆分: convert {prep:.1f} ({prep/wall_ms*100:.0f}%)  "
          f"native编码 {nat:.1f} ({nat/wall_ms*100:.0f}%)  "
          f"mux {mux:.2f} ({mux/wall_ms*100:.0f}%)  其它 {other:.1f} ms")


if __name__ == "__main__":
    main()
