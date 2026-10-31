#!/usr/bin/env python3
"""图片序列（.toos）规格基准：产品档位（ADR-I011 tier）文件大小 + 解码速度。

口径（对齐 2026-09-12 图片链路对齐后的生产策略）：
- 档位：422-low / 422-medium / 422-high / 422-ultra（image_cli kTierPresets
  同源：qm3 qp63 / qm1 qp61 / qm1 qp58 / qm0 qp58，含量化矩阵）；
- 序列：tests/test_videos 真实素材 4K 帧 → 2K（swscale 缩放）/ 4K / 8K
  （2× 最近邻上采样，同 bench_v8_encode 口径），每序列 8 帧独立 .toos；
- 熵档：rans2+AQ（新图片默认，V7-R2）vs vlc+AQ（旧图片默认，V2）双口径；
- 文件大小：TPIM 信封逐帧实测（含 256B 固定开销）；
- 解码速度：ToposImageCodec.decode（生产 ToposImageSequenceSource 同路径，
  含 probe/PIXL 读/帧解码）逐帧计时，1 帧预热后取 median；fps = N/总时长。

输出 tools/toos_sequence_tiers_2026-09-12.json。
"""
from __future__ import annotations

import ctypes
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import av  # noqa: E402

from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
)
from src.shared.codec.topos_image_binding import get_image_codec  # noqa: E402

SOURCES = [
    ("prores422HQ", REPO / "tests/test_videos/4K_prores422HQ.mov"),
    ("dnxhrhqx", REPO / "tests/test_videos/4K_DNxHR HQX 10bit.mov"),
]
N_FRAMES = 8
FIRST_FRAME = 10
SPECS = [("2K", 1920, 1080), ("4K", None, None), ("8K", "x2", None)]
# 产品档位（image_cli kTierPresets 422 家族）：(tier_id, qmatrix_id, qp)
TIERS = [("422-low", 3, 63), ("422-medium", 1, 61),
         ("422-high", 1, 58), ("422-ultra", 0, 58)]
CHAINS = [("rans2", 8), ("vlc", 1)]  # 新图片默认 vs 旧图片默认（均 AQ 开）


def _frame_planes(frame) -> list[np.ndarray]:
    pls = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2")
        stride = p.line_size // 2
        pls.append(arr.reshape(p.height, stride)[:, :p.width].copy())
    return pls


def _planes_for(frame, spec, rw, rh) -> list[np.ndarray]:
    if spec == "8K":
        pls = _frame_planes(frame.reformat(format="yuv422p10le"))
        return [np.ascontiguousarray(np.repeat(np.repeat(p, 2, 0), 2, 1))
                for p in pls]
    if rw is not None:
        return _frame_planes(frame.reformat(width=rw, height=rh,
                                            format="yuv422p10le"))
    return _frame_planes(frame.reformat(format="yuv422p10le"))


def _make_fc(w: int, h: int, qp: int, coding: int, qm: int) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = 3
    fc.pixel_format = 0
    fc.bit_depth = 10
    fc.qp_base = qp
    fc.qmatrix_id = qm  # 档位量化矩阵（low=3 medium/high=1 ultra=0）
    fc.reserved[0] = coding
    fc.reserved[1] = 1  # AQ 开（图片档策略，两熵档同口径）
    return fc


def main() -> int:
    codec = ToposCodec()
    ic = get_image_codec()
    recs = []

    for name, path in SOURCES:
        with av.open(str(path)) as cont:
            frames = []
            for i, frame in enumerate(cont.decode(video=0)):
                if FIRST_FRAME <= i < FIRST_FRAME + N_FRAMES:
                    frames.append(frame)
            assert len(frames) == N_FRAMES

        for spec, rw, rh in SPECS:
            plane_set = [_planes_for(f, spec, rw, rh) for f in frames]
            h, w = plane_set[0][0].shape
            data_frames = [[np.ascontiguousarray(p).tobytes() for p in pls]
                           for pls in plane_set]

            for tier, qm, qp in TIERS:
                for chain, coding in CHAINS:
                    seq_dir = Path(f"/tmp/toos_seq_{name}_{spec}_{tier}_{chain}")
                    seq_dir.mkdir(exist_ok=True)
                    sizes = []
                    enc_ms = []
                    wrap_ms = []
                    paths = []
                    for fi, data in enumerate(data_frames):
                        t0 = time.perf_counter()
                        pkt, st = codec.encode_frame(
                            _make_fc(w, h, qp, coding, qm), data)
                        enc_ms.append((time.perf_counter() - t0) * 1e3)
                        p = seq_dir / f"frame_{fi:04d}.toos"
                        t0 = time.perf_counter()
                        ic.write_packet(str(p), pkt, 0, durable=False)
                        wrap_ms.append((time.perf_counter() - t0) * 1e3)
                        sizes.append(p.stat().st_size)
                        paths.append(str(p))
                    # 解码：逐帧计时（生产绑定路径），首帧预热不计
                    ic.decode(paths[0])
                    dec_ms = []
                    for p in paths:
                        t0 = time.perf_counter()
                        planes, info, _ = ic.decode(p)
                        dec_ms.append((time.perf_counter() - t0) * 1e3)
                        assert len(planes) == 3
                    rec = {
                        "source": name, "spec": spec, "w": w, "h": h,
                        "tier": tier, "qmatrix": qm, "qp": qp,
                        "chain": chain, "frames": N_FRAMES,
                        "size_per_frame_mb": statistics.mean(sizes) / 1e6,
                        "sequence_mb": sum(sizes) / 1e6,
                        "bits_per_pixel": statistics.mean(sizes) * 8.0 / (w * h),
                        "encode_ms_median": statistics.median(enc_ms),
                        "encode_fps": N_FRAMES / (sum(enc_ms) / 1e3),
                        "encode_mps_in": w * h / (statistics.median(enc_ms) / 1e3) / 1e6,
                        "wrap_ms_median": statistics.median(wrap_ms),
                        "decode_ms_median": statistics.median(dec_ms),
                        "decode_ms_min": min(dec_ms),
                        "decode_ms_max": max(dec_ms),
                        "decode_fps": N_FRAMES / (sum(dec_ms) / 1e3),
                    }
                    recs.append(rec)
                    print(f"{name:11} {spec} {tier:11} {chain:6}: "
                          f"{rec['size_per_frame_mb']:8.2f}MB/帧 "
                          f"enc {rec['encode_ms_median']:7.1f}ms "
                          f"({rec['encode_fps']:5.1f}fps, "
                          f"{rec['encode_mps_in']:6.1f}MP/s) "
                          f"dec {rec['decode_ms_median']:6.1f}ms "
                          f"({rec['decode_fps']:5.1f}fps)")

    out = REPO / "tools/toos_sequence_tiers_2026-09-12.json"
    out.write_text(json.dumps(recs, indent=1))
    print(f"\nJSON → {out}")

    print("\n==== 按档位汇总（4K，双素材均值）：rans2 vs vlc ====")
    by = {}
    for r in recs:
        if r["spec"] == "4K":
            by.setdefault(r["tier"], {}).setdefault(r["chain"], []).append(r)
    for tier in [t for t, _, _ in TIERS]:
        a = by[tier]["rans2"]
        b = by[tier]["vlc"]
        am = statistics.mean(x["size_per_frame_mb"] for x in a)
        bm = statistics.mean(x["size_per_frame_mb"] for x in b)
        adm = statistics.mean(x["decode_ms_median"] for x in a)
        bdm = statistics.mean(x["decode_ms_median"] for x in b)
        print(f"{tier:11}: 尺寸 {100.0*(am-bm)/bm:+6.2f}%  "
              f"解码 {100.0*(adm-bdm)/bdm:+6.2f}%  "
              f"({bm:.1f}→{am:.1f}MB, {bdm:.1f}→{adm:.1f}ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
