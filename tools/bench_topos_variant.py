#!/usr/bin/env python3
"""画质优化变体基准：一次跑齐 体积 + 编码 fps + 解码 fps + 画质指标。

变体通过覆盖档位注册表条目注入（qmatrix_id / chroma_qp_offset），
走产品 ToposVideoEncoder 全链路（rate 控制按档位 bpp 定标）——性能与
体积数字即为用户导出体验；画质对齐母版 reformat 参照。

用法：
  .venv/bin/python tools/bench_topos_variant.py --variant v0 --tiers standard
  .venv/bin/python tools/bench_topos_variant.py --list
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
from scipy.ndimage import binary_dilation, laplace, sobel

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from src.shared.codec import topos_profiles  # noqa: E402
from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
)
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402
from src.shared.export.video_encoder import ColorMetadataConfig  # noqa: E402
from diagnose_edge_jaggies import (  # noqa: E402
    MEDIA,
    _plane_array,
    grid_signature,
)

MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR = REPO / "bench_out" / "edge_jag" / "variants"
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}
PIX_FMT = {"422": "yuv422p10le", "444": "yuv444p10le"}
FRAMES = 487
FPS = (25, 1)


class PlanarFrame:
    """ToposVideoEncoder 消费的最小产品帧协议（planar 直通）。"""

    def __init__(self, planes, pix_fmt, width, height, index):
        self.planes = planes
        self.pixel_format = pix_fmt
        self.width = width
        self.height = height
        self.index = index


# ————————————————————————— 变体定义 —————————————————————————

def _tier_patch(tier_id: str, **kw) -> None:
    base = topos_profiles.TOPOS_PROFILE_TIERS[tier_id]
    topos_profiles.TOPOS_PROFILE_TIERS[tier_id] = dataclasses.replace(base, **kw)


VARIANTS: dict[str, dict] = {
    # v0 = 产品现状——注意：注册表默认已切 id=4 后，v0 亦为 id4！
    # 需要真 flat 基线用 "vf"（强制回 0）。
    "v0": {},
    "vf": {"patch": {t: dict(qmatrix_id=0) for t in
                     ("netproxy", "proxy", "lt", "standard", "hq")}},
    "v1": {"patch": {"standard": dict(chroma_qp_offset=-2), "proxy": dict(chroma_qp_offset=-2), "hq": dict(chroma_qp_offset=-2), "lt": dict(chroma_qp_offset=-2)}},
    "v2": {"patch": {"standard": dict(chroma_qp_offset=-4), "proxy": dict(chroma_qp_offset=-4), "hq": dict(chroma_qp_offset=-4), "lt": dict(chroma_qp_offset=-4)}},
    # v3 = 边缘均衡矩阵（低频略粗 / 高频略细）
    "v3": {"patch": {"standard": dict(qmatrix_id=4), "proxy": dict(qmatrix_id=4), "hq": dict(qmatrix_id=4), "lt": dict(qmatrix_id=4)}},
    "v4": {"patch": {"standard": dict(qmatrix_id=4, chroma_qp_offset=-2), "proxy": dict(qmatrix_id=4, chroma_qp_offset=-2), "hq": dict(qmatrix_id=4, chroma_qp_offset=-2), "lt": dict(qmatrix_id=4, chroma_qp_offset=-2)}},
}


def apply_variant(name: str) -> None:
    for tier_id, kw in VARIANTS[name].get("patch", {}).items():
        _tier_patch(tier_id, **kw)


# ————————————————————————— 编码 —————————————————————————

def encoder_config(size: str, tier_id: str, sub: str) -> SimpleNamespace:
    w, h = SIZES[size]
    return SimpleNamespace(
        width=w, height=h, fps=FPS,
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


def run_encode(size: str, tier_id: str, out: Path) -> tuple[float, int]:
    w, h = SIZES[size]
    sub = "444" if tier_id in ("4444", "4444xq") else "422"
    pf = PIX_FMT[sub]
    enc = ToposVideoEncoder(str(out), encoder_config(size, tier_id, sub),
                            slice_threads=None, slice_rows=None)
    t0 = time.perf_counter()
    enc.open()
    n = 0
    with av.open(str(MASTER)) as c:
        s = c.streams.video[0]
        for frame in c.decode(s):
            if n >= FRAMES:
                break
            if (frame.width, frame.height) != (w, h) or frame.format.name != pf:
                frame = frame.reformat(width=w, height=h, format=pf)
            enc.encode_frame(PlanarFrame(
                tuple(_plane_array(p) for p in frame.planes[:3]), pf, w, h, n))
            n += 1
    enc.close()
    dt = time.perf_counter() - t0
    return dt, out.stat().st_size


def run_decode(path: Path) -> tuple[float, int]:
    codec = ToposCodec()
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    views = None
    t0 = time.perf_counter()
    n = 0
    try:
        total = int(movie.info().sample_count)
        for i in range(total):
            buf, size_b = movie.packet_into(i)
            fi = decoder.prepare(buf, size_b)
            if views is None:
                planes = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(planes)
            decoder.decode_views(buf, views, int(fi.plane_count), size_b)
            n += 1
    finally:
        dt = time.perf_counter() - t0
        decoder.close()
        movie.close()
    return dt, n


# ————————————————————————— 画质 —————————————————————————

def iter_master_planes(fmt: str, w: int, h: int, want: set[int]):
    out: dict[int, tuple] = {}
    with av.open(str(MASTER)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if i > max(want):
                break
            if (f.width, f.height) != (w, h) or f.format.name != fmt:
                f = f.reformat(width=w, height=h, format=fmt)
            if i in want:
                out[i] = tuple(_plane_array(p) for p in f.planes[:3])
    return out


def iter_file_planes(codec, path: Path, fmt: str, want: set[int]):
    out: dict[int, tuple] = {}
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    views = None
    planes = None
    try:
        total = int(movie.info().sample_count)
        for i in range(total):
            buf, size_b = movie.packet_into(i)
            fi = decoder.prepare(buf, size_b)
            if views is None:
                planes = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(planes)
            decoder.decode_views(buf, views, int(fi.plane_count), size_b)
            if i in want:
                out[i] = tuple(np.array(p) for p in planes[:3])
            if i > max(want):
                break
    finally:
        decoder.close()
        movie.close()
    return out


def quality(ref: dict, tst: dict) -> dict:
    acc: dict[str, list] = {}
    for i in sorted(ref):
        ry, ty = ref[i][0], tst[i][0]
        f = ry.astype(np.float64)
        mag = np.hypot(sobel(f, 0), sobel(f, 1))
        mask = binary_dilation(mag > max(30.0, float(np.percentile(mag, 96.0))), iterations=1)
        res = ty.astype(np.float64) - f
        row = {}
        for name, a, b in (("y", ry, ty), ("u", ref[i][1], tst[i][1]), ("v", ref[i][2], tst[i][2])):
            d = b.astype(np.float64) - a.astype(np.float64)
            mse = float(np.mean(d * d))
            row[f"{name}_psnr"] = 10 * np.log10(1023**2 / mse) if mse > 0 else 99.0
        d_e = res[mask]
        mse_e = float(np.mean(d_e * d_e))
        row["y_psnr_edge"] = 10 * np.log10(1023**2 / mse_e) if mse_e > 0 else 99.0
        le = float(np.mean(laplace(f)[mask] ** 2))
        lt = float(np.mean(laplace(ty.astype(np.float64))[mask] ** 2))
        row["sharp"] = lt / le if le > 0 else float("nan")
        row["grid"] = grid_signature(res, mask)
        for k, v in row.items():
            acc.setdefault(k, []).append(v)
    return {k: round(float(np.mean(v)), 3) for k, v in acc.items()}


# ————————————————————————— 主流程 —————————————————————————

def main() -> int:
    global FRAMES
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="v0")
    ap.add_argument("--size", default="2K", choices=list(SIZES))
    ap.add_argument("--tiers", nargs="+", default=["standard"])
    ap.add_argument("--frames", type=int, default=FRAMES)
    args = ap.parse_args()

    FRAMES = args.frames
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    apply_variant(args.variant)

    w, h = SIZES[args.size]
    want = {0, args.frames // 2, args.frames - 2}
    # 参照按档位族懒加载：混合 422/444 档位同批时各比各的（曾因整批
    # 单选 fmt 导致 422 解码色度 960 对 444 参照 1920 广播崩溃）。
    _refs: dict[str, dict] = {}

    def _ref_for(sub: str) -> dict:
        if sub not in _refs:
            _refs[sub] = iter_master_planes(PIX_FMT[sub], w, h, want)
        return _refs[sub]

    results = []
    for tier_id in args.tiers:
        sub = "444" if tier_id in ("4444", "4444xq") else "422"
        ref = _ref_for(sub)
        out = OUT_DIR / f"{args.variant}_{args.size}_{tier_id}.mov"
        enc_s, nbytes = run_encode(args.size, tier_id, out)
        enc_fps = n_enc = FRAMES / enc_s
        dec_s, n_dec = run_decode(out)
        # 音频不计入对比：解出视频净尺寸
        with av.open(str(out)) as c:
            st = c.streams.video[0]
            video_bytes = sum(p.buffer_size for p in c.demux(st))
        codec = ToposCodec()
        tst = iter_file_planes(codec, out, fmt, want)
        q = quality(ref, tst)
        row = {
            "variant": args.variant, "size": args.size, "tier": tier_id,
            "frames": args.frames, "video_bytes": video_bytes,
            "video_mbps": round(video_bytes * 8 / (args.frames / 25.0) / 1e6, 2),
            "enc_s": round(enc_s, 2), "enc_fps": round(enc_fps, 1),
            "dec_s": round(dec_s, 2), "dec_fps": round(n_dec / dec_s, 1),
            **q,
        }
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variant": args.variant,
        "results": results,
    }
    out_json = OUT_DIR / f"{args.variant}_{args.size}_bench.json"
    out_json.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"report → {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
