#!/usr/bin/env python3
"""M10-7C strict 搜索：真实素材探测预算与目标命中测量。

对每帧直接调 codec.encode_sized（tier target，qp 0..63），记录：
  - 每帧 probe 编码次数（tc_dev_sized_iters 增量，含 final）
  - bytes/target 命中误差（strict 语义：≤ target 的最小 QP → 误差 ∈ (−∞,0]）
  - 编码 fps（无 mux 的 native+binding 口径）

可与旧库交错对比：TOPOS_CODEC_LIB=/path/old.dylib python tools/bench_m107c_strict.py ...
"""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _load_frames(path: str, n: int):
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
        w = decoded[0].info.visible_width
        h = decoded[0].info.visible_height
        out = []
        for f in decoded:
            planes = [np.frombuffer(p, dtype=np.uint16) for p in f.planes]
            planes[0] = planes[0].reshape(h, w)
            for ci in (1, 2):
                if ci < len(planes):
                    planes[ci] = planes[ci].reshape(h, w // 2)
            out.append(tuple(planes))
        return out, w, h

    import av
    out = []
    with av.open(path) as cont:
        ctx = cont.streams.video[0].codec_context
        ctx.thread_count = 0
        ctx.thread_type = "FRAME"
        for i, frame in enumerate(cont.decode(video=0)):
            if i >= n:
                break
            fp = frame.planes
            h, w = fp[0].height, fp[0].width
            planes = []
            for p, pw in zip(fp, (w, w // 2, w // 2)):
                stride = p.line_size // 2
                arr = np.frombuffer(bytes(p), dtype="<u2").reshape(p.height, stride)
                planes.append(np.ascontiguousarray(arr[:, :pw]))
            out.append(tuple(planes))
    return out, w, h


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--no-cache", action="store_true",
                    help="禁用 M10-6.1 持久 enc_shared 缓存（A/B 对照）")
    ap.add_argument("--no-hint", action="store_true",
                    help="禁用 M10-6.3A 跨帧 qp 提示（A/B 对照）")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    from src.shared.codec import topos_binding as tb
    from src.shared.codec.topos_profiles import get_topos_tier
    from src.shared.export.topos_encoder import ToposVideoEncoder

    frames, w, h = _load_frames(args.input, args.frames + 4)
    warmup, meas = frames[:4], frames[4:]

    # 复用产品编码器的配置链（tier target / slice_threads / qp_base）
    out_tmp = Path("bench_out") / f"dbg_m107c_{w}x{h}.mov"
    out_tmp.parent.mkdir(exist_ok=True)
    tier = get_topos_tier("hq")
    cfg = SimpleNamespace(width=w, height=h, fps=Fraction(25, 1),
                          pix_fmt="yuv422p10le", profile="hq", crf=None,
                          has_alpha=False)
    enc = ToposVideoEncoder(str(out_tmp), cfg, slice_threads=args.threads)
    enc.open()
    target = enc._tier.frame_target_bytes(w, h)
    lib = enc._codec._lib
    lib.tc_dev_sized_reset.argtypes = []
    lib.tc_dev_sized_iters.restype = ctypes.c_uint64
    lib.tc_dev_set_enc_cache.argtypes = [ctypes.c_int]
    lib.tc_dev_set_qp_hint.argtypes = [ctypes.c_int]
    if args.no_cache:
        lib.tc_dev_set_enc_cache(ctypes.c_int(1))
    if args.no_hint:
        lib.tc_dev_set_qp_hint(ctypes.c_int(1))

    from src.shared.codec.topos_binding import _CFrameConfig
    fcfg = _CFrameConfig()
    ctypes.memmove(ctypes.byref(fcfg), ctypes.byref(enc._frame_cfg),
                   ctypes.sizeof(_CFrameConfig))

    codec = enc._codec
    to_bytes = lambda f: [np.ascontiguousarray(p).tobytes() for p in f]
    for f in warmup:
        codec.encode_sized(fcfg, to_bytes(f), target, qp_min=0, qp_max=63)

    lib.tc_dev_sized_reset()
    iters_list = []
    errs = []
    qps = []
    t0 = time.perf_counter()
    for f in meas:
        before = lib.tc_dev_sized_iters()
        pkt, stats, _qp = codec.encode_sized(fcfg, to_bytes(f), target,
                                             qp_min=0, qp_max=63)
        iters_list.append(lib.tc_dev_sized_iters() - before)
        errs.append((stats.packet_size - target) / target)
        qps.append(_qp)
    wall = time.perf_counter() - t0
    try:
        enc.close()
    except RuntimeError:
        out_tmp.unlink(missing_ok=True)  # 仅借用配置链，0 帧定稿拒绝属预期

    n = len(meas)
    tag = args.tag or Path(args.input).stem
    print(f"== {tag} {w}x{h} hq sized t{args.threads}，{n} 帧 ==")
    print(f"  fps {n / wall:.2f}  wall/帧 {wall / n * 1000:.1f} ms")
    print(f"  probe 编码次数: p50 {statistics.median(iters_list):.1f}  "
          f"p95 {sorted(iters_list)[int(len(iters_list) * 0.95) - 1]}  "
          f"max {max(iters_list)}  均值 {statistics.mean(iters_list):.2f}")
    under = sum(1 for e in errs if e <= 0.0)
    print(f"  bytes/target 误差: 中位 {statistics.median(errs) * 100:+.2f}%  "
          f"p95 {sorted(errs)[int(len(errs) * 0.95) - 1] * 100:+.2f}%  "
          f"最差 {min(errs) * 100:+.2f}%  超 target 帧数 {n - under}/{n}")
    print(f"  qp: min {min(qps)} 中位 {statistics.median(qps):.0f} max {max(qps)}")


if __name__ == "__main__":
    main()
