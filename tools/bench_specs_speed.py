#!/usr/bin/env python3
"""2K/4K 全规格 编码/解码 速度矩阵（口径 = native/topos_codec/tools/bench_tier_speed.py）。

与既有基准脚本的唯一差异：
- pf 由 family 决定（"422"→4:2:2，"444"→4:4:4），因此新增的 netproxy 档也能
  正确按 4:2:2 跑（原脚本用档位名白名单判 pf，不含 netproxy）；
- 显式对齐产品输出去块/QPT2（reserved[5]，与 ToposVideoEncoder._output_features
  同规则），并额外跑一遍关滤波以分离滤波的解码开销；
- 源：同分辨率 ProRes 4444XQ（与本次重导素材同源）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import tempfile
import time
from pathlib import Path

import av
import numpy as np

REPO = Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02")
TOOLS = REPO / "native" / "topos_codec" / "tools"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(TOOLS))

import bench_tier_speed as bts  # noqa: E402
from src.shared.codec.topos_binding import ToposCodec, ToposDecoder  # noqa: E402
from src.shared.export.topos_rate_control import ToposRateFeedback  # noqa: E402
from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS  # noqa: E402

QP_MAX = 95
FPS = 25.0
PLAN = {
    "2K": dict(width=1920, height=1080, frames=240),
    "4K": dict(width=3840, height=2160, frames=60),
}
# 档位 → (family, 源像素格式, bit_depth)
TIER_PLAN = [
    ("netproxy", "422", "yuv422p10le", 10),
    ("proxy", "422", "yuv422p10le", 10),
    ("lt", "422", "yuv422p10le", 10),
    ("standard", "422", "yuv422p10le", 10),
    ("hq", "422", "yuv422p10le", 10),
    ("4444", "444", "yuv444p10le", 10),
    ("4444xq", "444", "yuv444p12le", 12),
]


def plane_array(plane) -> np.ndarray:
    """AV 平面 → 紧排 u16 数组（与 generate_topos_baseline_corpus 同实现）。"""
    arr = np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2),
    )
    return np.ascontiguousarray(arr)


def product_features(tier_id: str, w: int) -> int:
    """与 ToposVideoEncoder._output_features 同规则。"""
    feats = 1  # DEBLOCK：4:2:2 / 10-bit / profile 3
    if tier_id in ("standard", "hq") and w >= 3840:
        feats |= 2  # QPT2：仅 4K standard/hq
    return feats


def write_raw(res: str, fmt: str, n: int) -> Path:
    """4444XQ 源 → 紧排 planar raw（工具 mmap 口径）。"""
    p = PLAN[res]
    out = Path(tempfile.gettempdir()) / f"specspeed_{res}_{fmt}_{n}.yuv"
    with open(out, "wb") as f:
        with av.open(str(REPO / "tests/test_videos"
                   / f"{res}_prores4444XQ.mov")) as c:
            for i, fr in enumerate(c.decode(c.streams.video[0])):
                if i >= n:
                    break
                if fr.format.name != fmt:
                    fr = fr.reformat(format=fmt)
                for plane in fr.planes[:3]:
                    f.write(plane_array(plane).tobytes())
    return out


def tier_run(codec, src, w, h, n, tier_id, bd, family, features, threads=16):
    """产品码控路径单档测量（含/不含输出特性）。"""
    t = TOPOS_PROFILE_TIERS[tier_id]
    pf = 0 if family == "422" else 1
    dims = bts.plane_count_bytes(pf, w, h)
    per_frame_px = sum(hh * ww for hh, ww in dims)
    target = max(1, round(t.target_bpp * w * h / 8.0))
    fc = bts.make_fc(w, h, t.native_profile, pf, bd,
                     t.qmatrix_id, t.chroma_qp_offset)
    fc.reserved[5] = features
    fc.slice_rows = 16

    def frame_bytes(i: int) -> list:
        off = i * per_frame_px
        out, c = [], 0
        for hh, ww in dims:
            px = hh * ww
            out.append(np.ascontiguousarray(
                src[off + c:off + c + px], dtype="<u2").tobytes())
            c += px
        return out

    warm = frame_bytes(0)
    fc.qp_base = 40
    codec.encode_frame(fc, warm)

    rc = ToposRateFeedback(target, qp_max=QP_MAX)
    total = 0
    enc = 0.0
    qps = []
    pkts = []
    for i in range(n):
        fr = frame_bytes(i)
        t0 = time.perf_counter()
        if i == 0:
            pkt, st, qp_used = codec.encode_sized(fc, fr, target, 0, QP_MAX)
            rc.seed(qp_used, len(pkt))
            qp = qp_used
        else:
            fc.qp_base = rc.qp
            pkt, st = codec.encode_frame(fc, fr)
            qp = int(fc.qp_base)
            rc.note(len(pkt))
        enc += time.perf_counter() - t0
        qps.append(qp)
        total += len(pkt)
        pkts.append(pkt)
    bpp = total * 8.0 / (w * h * n)

    planes = [np.zeros(d, dtype="<u2") for d in dims]
    with ToposDecoder(codec) as dec:
        views = dec.make_views(planes)
        dec.decode_views(pkts[0], views, len(planes))
        t0 = time.perf_counter()
        for pkt in pkts:
            dec.decode_views(pkt, views, len(planes))
        dec_s = time.perf_counter() - t0
    return dict(tier=tier_id, bpp=bpp, target_bpp=t.target_bpp,
                ratio=bpp / t.target_bpp,
                mbps=total * 8.0 / (n / FPS) / 1e6,
                qp_mean=float(np.mean(qps)), qp_min=min(qps),
                qp_max_seen=max(qps),
                enc_fps=n / enc, enc_rt=(n / enc) / FPS,
                dec_fps=n / dec_s, dec_rt=(n / dec_s) / FPS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", nargs="+", default=["2K", "4K"],
                    choices=("2K", "4K"))
    ap.add_argument("--frames-2k", type=int, default=PLAN["2K"]["frames"],
                    help="2K 每档帧数（默认 240）")
    ap.add_argument("--frames-4k", type=int, default=PLAN["4K"]["frames"],
                    help="4K 每档帧数（默认 60；60 帧口径含线程池冷启动，"
                         "稳态口径用 240）")
    ap.add_argument("--out", type=Path,
                    default=REPO / "bench_out" / "reexport" / "spec_speed_matrix.json")
    args = ap.parse_args()
    codec = ToposCodec()
    tmp = Path(tempfile.mkdtemp(prefix="specspeed_"))
    results = {}
    for res in args.sizes:
        p = PLAN[res]
        n = args.frames_2k if res == "2K" else args.frames_4k
        w, h = p["width"], p["height"]
        results[res] = []
        for fmt in ("yuv422p10le", "yuv444p10le", "yuv444p12le"):
            tiers = [x for x in TIER_PLAN if x[2] == fmt]
            if not tiers:
                continue
            raw = write_raw(res, fmt, n)
            try:
                src = np.memmap(raw, dtype="<u2", mode="r")
                for tier_id, family, _fmt, bd in tiers:
                    feats = product_features(tier_id, w) if family == "422" else 0
                    on = tier_run(codec, src, w, h, n, tier_id, bd, family, feats)
                    off = tier_run(codec, src, w, h, n, tier_id, bd, family, 0)
                    anchor = bts.PRORES_ANCHOR.get(tier_id,
                                                   bts.PRORES_ANCHOR["proxy"])
                    ref = bts.prores_ref(raw, fmt, w, h, n, anchor, tmp)
                    row = dict(on, feats=feats, dec_fps_no_filter=off["dec_fps"],
                               dec_rt_no_filter=off["dec_rt"],
                               prores_fps=ref["fps"], prores_mbps=ref["mbps"],
                               speed_vs_prores=on["enc_fps"] / ref["fps"])
                    results[res].append(row)
                    print(f"[{res}] {tier_id:9s} {row['mbps']:7.1f}Mbps "
                          f"({row['ratio']:.3f}×T, qp均{row['qp_mean']:.0f}) "
                          f"| 编码 {row['enc_fps']:6.1f}fps ({row['enc_rt']:5.2f}×) "
                          f"| 解码 {row['dec_fps']:6.1f}fps ({row['dec_rt']:5.2f}×) "
                          f"[关滤波 {row['dec_fps_no_filter']:6.1f}] "
                          f"| prores_ks {row['prores_fps']:5.1f}fps → "
                          f"{row['speed_vs_prores']:5.2f}×", flush=True)
            finally:
                del src
                raw.unlink(missing_ok=True)
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"\nJSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
