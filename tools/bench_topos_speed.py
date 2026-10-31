#!/usr/bin/env python3
"""产品口径编解码速度基准：体积无关，只测 fps。

编码走 ToposVideoEncoder 产品全链路（注册表现状档位、slice_threads=None
产品默认线程），解码走 ToposDecoder 默认线程池（max_slice_workers=0，
与产品播放共享预算同口径）。不产画质指标——画质用
bench_topos_variant.py / diagnose_edge_jaggies.py。

用法：
  .venv/bin/python tools/bench_topos_speed.py --size 2K --tiers proxy hq
  .venv/bin/python tools/bench_topos_speed.py --size 2K --tiers proxy \
      --decode-workers 1 4 0   # 复用已编码文件做解码线程扫描
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import av

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
)
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402
from src.shared.export.video_encoder import ColorMetadataConfig  # noqa: E402
from diagnose_edge_jaggies import MEDIA, _plane_array  # noqa: E402

MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR = REPO / "bench_out" / "edge_jag" / "speed"
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}
PIX_FMT = {"422": "yuv422p10le", "444": "yuv444p10le", "44412": "yuv444p12le"}


def tier_sub(tier_id: str) -> str:
    """4444xq（profile 6）要求 12-bit 主位深，其余 444 档 10-bit。"""
    if tier_id == "4444xq":
        return "44412"
    return "444" if tier_id in ("4444",) else "422"
ALL_TIERS = ("proxy", "lt", "standard", "hq", "4444", "4444xq")


class PlanarFrame:
    """ToposVideoEncoder 消费的最小产品帧协议（planar 直通）。"""

    def __init__(self, planes, pix_fmt, width, height, index):
        self.planes = planes
        self.pixel_format = pix_fmt
        self.width = width
        self.height = height
        self.index = index


def encoder_config(size: str, tier_id: str, sub: str) -> SimpleNamespace:
    w, h = SIZES[size]
    return SimpleNamespace(
        width=w, height=h, fps=(25, 1),
        pix_fmt=PIX_FMT[sub],
        profile=tier_id,
        crf=None,
        has_alpha=False,
        entropy_mode="rans2",
        aq_mode="off",
        rdo_mode="off",
        gop="intra",
        color_metadata=ColorMetadataConfig(),
    )


def run_encode(size: str, tier_id: str, out: Path,
               frames: int) -> tuple[float, int]:
    w, h = SIZES[size]
    sub = tier_sub(tier_id)
    pf = PIX_FMT[sub]
    enc = ToposVideoEncoder(str(out), encoder_config(size, tier_id, sub),
                            slice_threads=None, slice_rows=None)
    t0 = time.perf_counter()
    enc.open()
    n = 0
    with av.open(str(MASTER)) as c:
        s = c.streams.video[0]
        for frame in c.decode(s):
            if n >= frames:
                break
            if (frame.width, frame.height) != (w, h) or frame.format.name != pf:
                frame = frame.reformat(width=w, height=h, format=pf)
            enc.encode_frame(PlanarFrame(
                tuple(_plane_array(p) for p in frame.planes[:3]), pf, w, h, n))
            n += 1
    enc.close()
    dt = time.perf_counter() - t0
    return dt, out.stat().st_size


def run_decode(codec: ToposCodec, path: Path, workers: int,
               frames: int) -> tuple[float, int]:
    movie = ToposMovieFile(codec, str(path))
    total = min(movie.info().sample_count, frames)
    decoder = ToposDecoder(codec, max_slice_workers=workers)
    views = None
    try:
        t0 = time.perf_counter()
        for i in range(total):
            pkt = movie.packet(i)
            if views is None:
                fi = decoder.prepare(pkt)
                import numpy as np
                h, w = fi.visible_height, fi.visible_width
                pw = w // 2 if fi.pixel_format == 0 else w
                arrs = [np.empty((h, w), "<u2"),
                        np.empty((h, pw), "<u2"),
                        np.empty((h, pw), "<u2")]
                views = decoder.make_views(arrs)
            decoder.decode_views(pkt, views, 3)
        dt = time.perf_counter() - t0
    finally:
        decoder.close()
        movie.close()
    return dt, total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", choices=("2K", "4K"), default="2K")
    ap.add_argument("--tiers", nargs="+", default=list(ALL_TIERS))
    ap.add_argument("--frames", type=int, default=487)
    ap.add_argument("--decode-workers", nargs="+", type=int, default=None,
                    help="解码线程扫描（复用已编码文件）；缺省只测默认值 0")
    ap.add_argument("--master", default=None,
                    help="喂入源（缺省 4K 母版）；2K 档配 2K 母版即"
                         "「同尺寸源导出」口径，免逐帧缩放开销")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    global MASTER
    if args.master:
        MASTER = Path(args.master)

    results = []
    codec = ToposCodec()
    for tier in args.tiers:
        out = OUT_DIR / f"{args.size}_{tier}.mov"
        enc_s, size_b = run_encode(args.size, tier, out, args.frames)
        enc_fps = args.frames / enc_s
        row = {
            "size": args.size, "tier": tier,
            "frames": args.frames,
            "enc_s": round(enc_s, 2), "enc_fps": round(enc_fps, 1),
            "video_mb": round(size_b / (1 << 20), 1),
            "decode": {},
        }
        workers_list = args.decode_workers if args.decode_workers else [0]
        for wk in workers_list:
            dec_s, n = run_decode(codec, out, wk, args.frames)
            row["decode"][f"w{wk}"] = {
                "fps": round(n / dec_s, 1), "ms_per_frame": round(1000 * dec_s / n, 2)}
        results.append(row)
        print(f"{args.size}/{tier}: encode {enc_fps:.1f} fps "
              f"({row['video_mb']} MB)  decode " +
              "  ".join(f"w{wk}={row['decode'][f'w{wk}']['fps']:.0f}fps"
                        for wk in workers_list), flush=True)

    out_json = Path(args.json) if args.json else OUT_DIR / f"speed_{args.size}.json"
    # 增量合并：多批运行（含同档不同线程数的解码扫描）共存于一份报告。
    merged: dict = {}
    if out_json.exists():
        try:
            for r0 in json.loads(out_json.read_text()).get("results", []):
                merged[(r0["size"], r0["tier"])] = r0
        except (json.JSONDecodeError, KeyError):
            pass
    for r in results:
        k = (r["size"], r["tier"])
        if k in merged:
            old = merged[k]
            dec = dict(old.get("decode", {}))
            dec.update(r["decode"])
            old.update(r)
            old["decode"] = dec
        else:
            merged[k] = r
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "master": str(MASTER),
        "machine_cpus": __import__("os").cpu_count(),
        "results": list(merged.values()),
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"report → {out_json}")


if __name__ == "__main__":
    main()
