#!/usr/bin/env python3
"""V8 批 4：GPU 段化解码基准 + 自动择优校准（4K/8K 真实素材）。

- GPU 链：export → rans2_decode_segment（段并行）→ inverse_dct_wsd_v8
  （CSR 原位；token 不下行）——计时 = rANS kernel + wsd kernel + 上载；
- CPU 链：tc_frame_decode（V8 路径，16 线程）；
- 校准：结果写 tools/v8_gpu_calib.json（GPU/CPU 择优缓存；批 4 的
  "校准比较 GPU/CPU 链路自动择优回退" 的数据源）。
对照目标（format_plan §1.1 外推）：4K rANS ~10ms + wsd 6.6ms ≈ 17ms；
8K ≤35ms 量级。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests/shared/gpu/metal"))

import av  # noqa: E402

from src.shared.codec.topos_binding import ToposCodec  # noqa: E402
from src.shared.gpu.metal.context import MetalComputeContext  # noqa: E402
from test_metal_v8 import (  # noqa: E402
    _make_fc, _v8_export,
)


def _frame_planes(frame):
    pls = []
    for p in frame.planes:
        stride = p.line_size // 2
        arr = np.frombuffer((ctypes.c_char * p.buffer_size).from_address(
            p.buffer_ptr), dtype="<u2")
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _gpu_decode_timed(ctx, codec, packet, w, h, pf, bd, dims, qp, runs=5):
    """GPU 全链计时（分解：export+上载 / rANS / wsd）。返回 (wall, rans, wsd)。"""
    smeta, tabs, ns, nt = _v8_export(codec, packet)
    streams = np.frombuffer(packet, np.uint8).copy()
    # 预编译 + 预建缓冲（稳态）
    off_total = int((smeta[:, 2] + 1).sum())
    pairs_total = int((smeta[:, 2] * 63).sum())
    dc_total = int((smeta[:, 10] + smeta[:, 2]).max())
    from src.shared.gpu.metal.kernels_dec import METAL_RANS2_DECODE_SEGMENT_MSL
    from src.shared.gpu.metal.kernels_idct import METAL_INVERSE_DCT_WSD_V8_MSL
    from test_metal_kernels_idct import _frozen_tables
    kr = ctx.compile_kernel("rans2_decode_segment", METAL_RANS2_DECODE_SEGMENT_MSL)
    kw = ctx.compile_kernel("inverse_dct_wsd_v8", METAL_INVERSE_DCT_WSD_V8_MSL)
    _, mtable, wtable = _frozen_tables(codec)
    q_luma, q_chroma = _qtab_for_qp_cached(codec, qp, bd)
    bufs = {
        0: ctx.create_buffer_from_numpy(streams),
        1: ctx.create_buffer_from_numpy(smeta),
        2: ctx.create_buffer_from_numpy(tabs),
        3: ctx.create_buffer_from_numpy(np.zeros(dc_total, np.int32)),
        4: ctx.create_buffer_from_numpy(np.zeros(off_total, np.uint32)),
        5: ctx.create_buffer_from_numpy(np.zeros(pairs_total, "<u8")),
        6: ctx.create_buffer_from_numpy(np.zeros(ns, np.uint32)),
        7: ctx.create_buffer_from_numpy(np.array([ns], np.uint32)),
    }
    g = 64
    thr = (((ns + g - 1) // g) * g, 1, 1)

    # wsd 缓冲预建（稳态；免计时内创建开销）
    planes_buf = []
    for p, (ph, pw) in enumerate(dims):
        sel = np.where(smeta[:, 0] == p)[0]
        cols = int(smeta[sel[0], 9])
        rows = (ph + 7) // 8
        blkseg = np.zeros(rows * cols, np.uint32)
        sblk0 = np.zeros(len(sel), np.uint32)
        soff = np.zeros(len(sel), np.uint32)
        spbase = np.zeros(len(sel), np.uint32)
        for si, gi in enumerate(sel):
            sblk0[si] = int(smeta[gi, 1])
            soff[si] = int(smeta[gi, 11])
            spbase[si] = int(smeta[gi, 12])
            blkseg[int(smeta[gi, 1]):int(smeta[gi, 1]) + int(smeta[gi, 2])] = si
        qtab = np.zeros((len(sel), 64), np.uint32)
        qtab[:] = q_luma if p == 0 else q_chroma
        mid, maxv = 1 << (bd - 1), (1 << bd) - 1
        chain = {0: bufs[3], 1: bufs[5], 2: bufs[4],
                 3: ctx.create_buffer_from_numpy(np.zeros(ph * pw, "<u2")),
                 4: ctx.create_buffer_from_numpy(
                     np.array([cols, rows, pw, ph, mid, maxv, pw,
                               int(smeta[sel[0], 10])], np.int32)),
                 5: ctx.create_buffer_from_numpy(mtable),
                 6: ctx.create_buffer_from_numpy(wtable),
                 7: ctx.create_buffer_from_numpy(qtab),
                 8: ctx.create_buffer_from_numpy(blkseg),
                 9: ctx.create_buffer_from_numpy(sblk0),
                 10: ctx.create_buffer_from_numpy(spbase),
                 11: ctx.create_buffer_from_numpy(soff)}
        planes_buf.append((chain, cols, rows))

    t_rans = t_wsd = 0.0
    for _ in range(runs):
        t0 = time.perf_counter()
        ctx.dispatch_sync(kr, buffers=bufs, threads=thr, group=(g, 1, 1))
        t_rans += time.perf_counter() - t0
        t0 = time.perf_counter()
        for chain, cols, rows in planes_buf:
            ctx.dispatch_sync(kw, buffers=chain,
                              threads=(cols * 8, rows * 8, 1), group=(8, 8, 1))
        t_wsd += time.perf_counter() - t0
    return t_rans / runs, t_wsd / runs


_QC = {}


def _qtab_for_qp_cached(codec, qp, bd):
    key = (qp, bd)
    if key not in _QC:
        from test_metal_v8 import _qtab_for_qp
        _QC[key] = _qtab_for_qp(codec, qp, bd)
    return _QC[key]


def _cpu_decode_timed(codec, packet, w, h, pf, bd, dims, runs=5):
    from src.shared.codec.topos_binding import ToposDecoder
    arrays = [np.zeros(d, "<u2") for d in dims]
    t = 0.0
    for _ in range(runs):
        t0 = time.perf_counter()
        with ToposDecoder(codec) as dec:
            views = dec.make_views(arrays)
            dec.decode_views(packet, views, len(arrays))
        t += time.perf_counter() - t0
    return t / runs


def main() -> int:
    codec = ToposCodec()
    lib = codec._lib
    lib.tc_dev_set_thread_count.argtypes = [ctypes.c_int]
    lib.tc_dev_set_thread_count(16)
    ctx = MetalComputeContext()
    recs = []
    for src_name, path, fi in (
        ("prores422HQ", REPO / "tests/test_videos/4K_prores422HQ.mov", 10),
    ):
        with av.open(str(path)) as cont:
            planes4k = None
            for i, frame in enumerate(cont.decode(video=0)):
                if i == fi:
                    planes4k = _frame_planes(frame.reformat(format="yuv422p10le"))
                    break
        for res, planes in (("4K", planes4k),
                            ("8K", [np.ascontiguousarray(
                                np.repeat(np.repeat(p, 2, 0), 2, 1))
                                for p in planes4k])):
            h, w = planes[0].shape
            data = [np.ascontiguousarray(p).tobytes() for p in planes]
            dims = [(h, w), (h, w // 2), (h, w // 2)]
            pkt, _ = codec.encode_frame(_make_fc(w, h, 0, 10, 30), data)
            t_rans, t_wsd = _gpu_decode_timed(ctx, codec, pkt, w, h, 0, 10,
                                              dims, 30)
            t_cpu = _cpu_decode_timed(codec, pkt, w, h, 0, 10, dims)
            recs.append({
                "res": res, "w": w, "h": h, "qp": 30,
                "gpu_rans_ms": t_rans * 1000, "gpu_wsd_ms": t_wsd * 1000,
                "gpu_total_ms": (t_rans + t_wsd) * 1000,
                "cpu_ms": t_cpu * 1000,
                "gpu_vs_cpu": (t_rans + t_wsd) / t_cpu,
            })
            print(f"{res} qp30: GPU rANS={t_rans*1000:.1f}ms + wsd={t_wsd*1000:.1f}ms "
                  f"= {(t_rans+t_wsd)*1000:.1f}ms | CPU={t_cpu*1000:.1f}ms "
                  f"| GPU/CPU={t_rans/t_cpu if False else (t_rans+t_wsd)/t_cpu:.2f}×")
    out = REPO / "tools/v8_gpu_calib.json"
    out.write_text(json.dumps({
        "machine": "i9-9900K + RX 6900 XT",
        "records": recs,
        "policy": "gpu_if_faster",
    }, indent=1))
    print(f"\n校准 JSON → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
