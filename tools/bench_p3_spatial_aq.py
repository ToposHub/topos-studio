#!/usr/bin/env python3
"""P3 空间 AQ A/B 基准（画质对齐计划 §5 P3 验收）。

同档位同码率（tier bpp 码控）下 AQ off vs on：

- 编码全片 487 帧（产品 ToposVideoEncoder 路径），计时编码吞吐；
- 抽样帧度量 Y/U/V PSNR、SSIM、MAE、8×8 块边界残差指数（母版参考，
  与 tools/measure_topos_baseline_quality.py 同口径）；
- 记录总字节（同码率约束验证）。

验收（计划 §5 P3）：Proxy/LT 同码率客观指标提高、块残差下降、
编码/解码速度不突破预算。

用法::

    .venv/bin/python tools/bench_p3_spatial_aq.py            # 2K/4K × proxy/lt
    .venv/bin/python tools/bench_p3_spatial_aq.py --stride 10 --tiers proxy
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from fractions import Fraction

import av
import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.path.insert(0, str(REPO / "tools"))

from measure_topos_baseline_quality import (  # noqa: E402
    FRAMES,
    SIZES,
    Metric,
    iter_reference,
    iter_topos,
    sample_indices,
)
from src.shared.codec.color_metadata_config import ColorMetadataConfig  # noqa: E402
from src.shared.codec.topos_binding import ToposCodec  # noqa: E402
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402

MEDIA = REPO / "tests" / "test_videos"
OUT_DIR = REPO / "bench_out" / "topos_quality"
FPS = Fraction(25, 1)


class PlanarFrame:
    def __init__(self, planes, fmt, w, h, idx):
        self.planes, self.pixel_format = planes, fmt
        self.width, self.height, self.frame_index = w, h, idx
        self.is_planar = True


def _plane_array(plane) -> np.ndarray:
    return np.ascontiguousarray(np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2)))


def encode_variant(size: str, tier: str, aq: bool, threads: int) -> tuple:
    """同档位码控编码 487 帧 → (path, encode_fps, video_bytes)。"""
    w, h = SIZES[size]
    src = MEDIA / f"{size}_prores{'422Proxy' if tier == 'proxy' else '422LT'}.mov"
    out = OUT_DIR / f"p3_{size}_{tier}_{'aq' if aq else 'off'}.mov"
    out.unlink(missing_ok=True)
    cfg = SimpleNamespace(
        width=w, height=h, fps=FPS, pix_fmt="yuv422p10le", profile=tier,
        crf=None, has_alpha=False, entropy_mode="rans2",
        aq_mode="on" if aq else "off", rdo_mode="off", gop="intra",
        color_metadata=ColorMetadataConfig())
    enc = ToposVideoEncoder(str(out), cfg, slice_threads=threads)
    enc.open()
    t0 = time.perf_counter()
    try:
        n = 0
        with av.open(str(src)) as c:
            for f in c.decode(c.streams.video[0]):
                planes = tuple(_plane_array(p) for p in f.planes[:3])
                enc.encode_frame(PlanarFrame(planes, "yuv422p10le", w, h, n))
                n += 1
        assert n == FRAMES
    finally:
        enc.close()
    dt = time.perf_counter() - t0
    codec = ToposCodec()
    from src.shared.codec.topos_binding import ToposMovieFile
    m = ToposMovieFile(codec, str(out))
    video_bytes = sum(m.packet_into(i)[1] for i in range(FRAMES))
    m.close()
    return out, FRAMES / dt, video_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stride", type=int, default=20)
    parser.add_argument("--tiers", default="proxy,lt")
    parser.add_argument("--only", action="append", default=[],
                        choices=sorted(SIZES))
    parser.add_argument("--threads", type=int, default=16)
    args = parser.parse_args()

    sizes = list(args.only) or list(SIZES)
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    sampled = set(sample_indices(max(1, args.stride)))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codec = ToposCodec()

    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "stride": args.stride, "results": {}}
    print(f"P3 空间 AQ A/B：{sizes} × {tiers}，抽样 {len(sampled)} 帧", flush=True)

    for size in sizes:
        variants = {}
        for tier in tiers:
            for aq in (False, True):
                path, fps_enc, vbytes = encode_variant(size, tier, aq,
                                                       args.threads)
                variants[(tier, aq)] = (path, fps_enc, vbytes)
                print(f"  编码 {size}/{tier}/{'aq' if aq else 'off'}: "
                      f"{fps_enc:.1f} fps, video {vbytes / 1e6:.1f} MB",
                      flush=True)
        # 度量：参考流一次，四路 topos 锁步
        streams = {k: iter_topos(codec, v[0]) for k, v in variants.items()}
        metrics = {k: Metric("yuv422p10le") for k in variants}
        t_dec0 = time.perf_counter()
        n_dec = 0
        for i, ref in iter_reference(size, "yuv422p10le"):
            for k in variants:
                _, tp = next(streams[k])
                n_dec += 1
                if i in sampled:
                    metrics[k].add(ref, tp)
        dec_fps = n_dec / (time.perf_counter() - t_dec0)
        for tier in tiers:
            off, on = metrics[(tier, False)].result(), metrics[(tier, True)].result()
            entry = {
                "encode_fps_off": round(variants[(tier, False)][1], 1),
                "encode_fps_aq": round(variants[(tier, True)][1], 1),
                "video_bytes_off": variants[(tier, False)][2],
                "video_bytes_aq": variants[(tier, True)][2],
                "decode_fps_combined": round(dec_fps / len(variants), 1),
                "off": {k: round(v, 4) for k, v in off.items()},
                "aq": {k: round(v, 4) for k, v in on.items()},
                "delta": {f"psnr_y": round(on["psnr_y"] - off["psnr_y"], 3),
                          f"psnr_u": round(on["psnr_u"] - off["psnr_u"], 3),
                          f"psnr_v": round(on["psnr_v"] - off["psnr_v"], 3),
                          "ssim_y": round(on["ssim_y"] - off["ssim_y"], 5),
                          "block_y": round(on["block_index_y"]
                                           - off["block_index_y"], 4),
                          "mae_y": round(on["mae_y"] - off["mae_y"], 3)},
            }
            report["results"][f"{size}/{tier}"] = entry
            rb = on["psnr_y"] - off["psnr_y"]
            rblk = on["block_index_y"] - off["block_index_y"]
            rssim = on["ssim_y"] - off["ssim_y"]
            same_rate = abs(entry["video_bytes_aq"]
                            / entry["video_bytes_off"] - 1.0)
            print(f"  {size}/{tier}: ΔPSNR_Y={rb:+.2f} dB  ΔSSIM={rssim:+.5f}"
                  f"  Δblock={rblk:+.3f}  码率差={same_rate * 100:+.2f}%"
                  f"  编码 {entry['encode_fps_off']}→{entry['encode_fps_aq']} fps",
                  flush=True)

    out_path = OUT_DIR / "p3_spatial_aq_ab.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"\n结果：{out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
