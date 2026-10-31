#!/usr/bin/env python3
"""TOPOS vs ProRes 同源画质对比台：一次跑齐 体积 + 编码/解码 fps + 三向指标 + 放大面板。

与 ``bench_topos_variant.py`` 的差别：
- 输入是 **4K ProRes 4444 母版**（``reformat`` 到目标尺寸 = ground truth），
  不是同档 ProRes ——两侧都是一代损失，衡量的是"转码高规格素材"这一
  真实产品路径；
- 同帧输出 **母版 / ProRes 同档 / TOPOS** 三方放大面板，主观与客观同源；
- 档位表补丁（qmatrix_id / chroma_qp_offset / target_bpp）通过命令行注入，
  不改仓库默认值。

用法::

    .venv/bin/python tools/bench_topos_qgain.py --tag base --tiers proxy
    .venv/bin/python tools/bench_topos_qgain.py --tag t1 --tiers proxy --patch proxy:qmatrix_id=4
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
from scipy.ndimage import (binary_dilation, laplace, maximum_filter1d,
                            minimum_filter1d, sobel)

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
    panel,
)

MASTER = MEDIA / "4K_prores4444.mov"
OUT_DIR = REPO / "bench_out" / "qgain"
SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}
PIX_FMT = {"422": "yuv422p10le", "444": "yuv444p10le", "444xq": "yuv444p12le"}
PRORES_NAME = {
    "netproxy": "2K_prores422Proxy.mov",
    "proxy": "2K_prores422Proxy.mov",
    "lt": "2K_prores422LT.mov",
    "standard": "2K_prores422.mov",
    "hq": "2K_prores422HQ.mov",
    "4444": "2K_prores4444.mov",
    "4444xq": "2K_prores4444XQ.mov",
}
FRAMES = 487
FPS = (25, 1)
#: 抽样帧：0（首帧）、100/300（既有取证口径）、486（末帧）。
SAMPLE_FRAMES = (0, 100, 300, 486)


def sub_of(tier_id: str) -> str:
    if tier_id == "4444xq":
        return "444xq"
    return "444" if tier_id == "4444" else "422"


class PlanarFrame:
    """ToposVideoEncoder 消费的最小产品帧协议（planar 直通）。"""

    def __init__(self, planes, pix_fmt, width, height, index):
        self.planes = planes
        self.pixel_format = pix_fmt
        self.width = width
        self.height = height
        self.index = index


# ————————————————————————— 档位补丁 —————————————————————————

def apply_patches(specs: list[str]) -> list[str]:
    applied = []
    for spec in specs:
        tier_id, _, rest = spec.partition(":")
        key, _, value = rest.partition("=")
        if not tier_id or not key:
            raise SystemExit(f"--patch 形式为 tier:field=value，收到 {spec!r}")
        base = topos_profiles.TOPOS_PROFILE_TIERS[tier_id]
        if not hasattr(base, key):
            raise SystemExit(f"档位 {tier_id} 无字段 {key}")
        cur = getattr(base, key)
        cast = type(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else str
        new = cast(value)
        topos_profiles.TOPOS_PROFILE_TIERS[tier_id] = dataclasses.replace(
            base, **{key: new})
        applied.append(f"{tier_id}.{key}: {cur!r}→{new!r}")
    return applied


def tier_snapshot(tier_id: str) -> dict:
    t = topos_profiles.TOPOS_PROFILE_TIERS[tier_id]
    return {k: getattr(t, k) for k in
            ("target_bpp", "chroma_qp_offset", "qmatrix_id")}


# ————————————————————————— 源 —————————————————————————

def iter_master(fmt: str, w: int, h: int, want: set[int]):
    """母版 reformat（bicubic，与既有取证同口径）→ {index: (Y,U,V)}。"""
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


def iter_prores(path: Path, fmt: str, w: int, h: int, want: set[int]):
    out: dict[int, tuple] = {}
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        for i, f in enumerate(c.decode(s)):
            if i > max(want):
                break
            if (f.width, f.height) != (w, h) or f.format.name != fmt:
                f = f.reformat(width=w, height=h, format=fmt)
            if i in want:
                out[i] = tuple(_plane_array(p) for p in f.planes[:3])
    return out


def iter_topos(codec: ToposCodec, path: Path, want: set[int]):
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


def decode_metrics(path: Path, frames: int) -> tuple[float, float, int]:
    """(解码秒数, fps, 视频净载荷字节)。"""
    codec = ToposCodec()
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    views = None
    n = 0
    t0 = time.perf_counter()
    try:
        total = int(movie.info().sample_count)
        for i in range(total):
            buf, size_b = movie.packet_into(i)
            fi = decoder.prepare(buf, size_b)
            if views is None:
                ps = []
                for k in range(int(fi.plane_count)):
                    pw, ph = codec.plane_geometry(fi, k)
                    ps.append(np.empty((ph, pw), dtype="<u2"))
                views = decoder.make_views(ps)
            decoder.decode_views(buf, views, int(fi.plane_count), size_b)
            n += 1
    finally:
        dt = time.perf_counter() - t0
        decoder.close()
        movie.close()
    with av.open(str(path)) as c:
        st = c.streams.video[0]
        video_bytes = sum(p.buffer_size for p in c.demux(st))
    return dt, (n / dt if dt > 0 else float("nan")), video_bytes


# ————————————————————————— 指标 —————————————————————————

def _psnr(a: np.ndarray, b: np.ndarray, mask=None) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    if mask is not None:
        d = d[mask]
    if d.size == 0:
        return float("nan")
    mse = float(np.mean(d * d))
    return float("nan") if mse <= 0 else 10.0 * np.log10(1023.0 ** 2 / mse)


def _edge_mask(ref: np.ndarray) -> np.ndarray:
    f = ref.astype(np.float64)
    mag = np.hypot(sobel(f, 0), sobel(f, 1))
    thr = max(30.0, float(np.percentile(mag, 96.0)))
    return binary_dilation(mag > thr, iterations=1)


def _grid(res: np.ndarray, mask: np.ndarray) -> float:
    h, w = res.shape
    yy, xx = np.mgrid[0:h, 0:w]
    g = (xx % 8 == 0) | (yy % 8 == 0)
    m = mask & ~g
    gg = mask & g
    ea = float(np.sum(res[mask] ** 2))
    if ea <= 0 or not m.any() or not gg.any():
        return float("nan")
    return (float(np.sum(res[gg] ** 2)) / ea) / (gg.sum() / float(mask.sum()))


def _stair(res: np.ndarray, mask: np.ndarray) -> float:
    gx, gy = sobel(res, 1), sobel(res, 0)
    mag = np.hypot(gx, gy)
    if not mask.any():
        return float("nan")
    sel = mask & (mag > np.percentile(mag[mask], 70))
    if not sel.any():
        return float("nan")
    return float(np.sum(np.minimum(np.abs(gx[sel]), np.abs(gy[sel])))
                 / np.sum(mag[sel]))


def _blockiness(ref: np.ndarray, tst: np.ndarray, flat_thr: float = 2.0,
                k: int = 3):
    """经典块效应度（GBIM 口径）：平坦区「跨 8×8 块界」相邻像素差 −「块内」相邻像素差。

    平坦区由**参照图**判定（±k 窗口范围 < flat_thr）——重构图自身带量化
    噪声与块界台阶，拿它判平坦会把要测的台阶本身排除掉；参照图是干净的，
    其平坦区里块界台阶真值恒 ~0，故重构图在此处的台阶量即伪影量。返回
    ((水平台阶量, 水平比), (垂直台阶量, 垂直比))，台阶量单位 LSB。
    """
    rf = ref.astype(np.float64)
    tf = tst.astype(np.float64)
    w = 2 * k + 1
    out = []
    for axis in (1, 0):
        d = np.abs(np.diff(tf, axis=axis))
        rng = (maximum_filter1d(rf, w, axis=axis)
               - minimum_filter1d(rf, w, axis=axis))
        n = d.shape[axis]
        sl = [slice(None)] * 2
        sl[axis] = slice(0, n)
        smooth = rng[tuple(sl)] < flat_thr
        idx = np.arange(n)
        on = ((idx + 1) % 8) == 0
        shape = [1, 1]
        shape[axis] = n
        on_b = np.broadcast_to(on.reshape(shape), d.shape)
        a = d[smooth & on_b]
        b = d[smooth & ~on_b]
        step = float(a.mean() - b.mean()) if a.size and b.size else float("nan")
        ratio = (float(a.mean() / b.mean())
                 if a.size and b.size and b.mean() > 0 else float("nan"))
        out.append((round(step, 3), round(ratio, 4)))
    return out


def _sharp(y: np.ndarray, mask: np.ndarray, ref_lap: float) -> float:
    l = laplace(y.astype(np.float64))
    return float(np.mean(l[mask] ** 2)) / ref_lap if ref_lap > 0 else float("nan")


def _sharp(y: np.ndarray, mask: np.ndarray, ref_lap: float) -> float:
    l = laplace(y.astype(np.float64))
    return float(np.mean(l[mask] ** 2)) / ref_lap if ref_lap > 0 else float("nan")


def quality(ref: dict, tst: dict) -> dict:
    acc: dict[str, list] = {}
    for i in sorted(ref):
        rp, tp = ref[i], tst[i]
        ry = rp[0]
        f = ry.astype(np.float64)
        mask = _edge_mask(ry)
        ref_lap = float(np.mean(laplace(f)[mask] ** 2))
        res = tp[0].astype(np.float64) - f
        (bh, bhr), (bv, bvr) = _blockiness(rp[0], tp[0])
        row = {
            "y_psnr": _psnr(rp[0], tp[0]),
            "u_psnr": _psnr(rp[1], tp[1]),
            "v_psnr": _psnr(rp[2], tp[2]),
            "y_psnr_edge": _psnr(rp[0], tp[0], mask),
            "y_mae_edge": float(np.mean(np.abs(res[mask]))),
            "sharp": _sharp(tp[0], mask, ref_lap),
            "block_h": bh, "block_v": bv,
            "block_h_ratio": bhr, "block_v_ratio": bvr,
            "grid": _grid(res, mask),
            "stair": _stair(res, mask),
        }
        for k, v in row.items():
            acc.setdefault(k, []).append(v)
    return {k: round(float(np.mean(v)), 3) for k, v in acc.items()}


def rgb_quality(ref: dict, tst: dict) -> dict:
    """4:2:2 → 4:4:4 上采样 + BT.709 limited → RGB 后的观感域指标。"""
    acc: dict[str, list] = {}
    for i in sorted(ref):
        out = []
        for who in (ref, tst):
            y, u, v = (p.astype(np.float32) for p in who[i])
            uf = np.repeat(u, 2, axis=1)[:, :y.shape[1]]
            vf = np.repeat(v, 2, axis=1)[:, :y.shape[1]]
            yl = (y - 64.0) / 876.0
            ul = (uf - 512.0) / 896.0
            vl = (vf - 512.0) / 896.0
            r = yl + 1.5748 * vl
            g = yl - 0.1873 * ul - 0.4681 * vl
            b = yl + 1.8556 * ul
            out.append(np.stack([r, g, b], axis=-1))
        d = out[0] - out[1]
        acc.setdefault("rgb_psnr", []).append(
            float(10.0 * np.log10(1.0 / max(float(np.mean(d * d)), 1e-12))))
        acc.setdefault("rgb_mae", []).append(float(np.mean(np.abs(d)) * 255.0))
    return {k: round(float(np.mean(v)), 3) for k, v in acc.items()}


# ————————————————————————— 主流程 —————————————————————————

def encode(size: str, tier_id: str, out: Path, frames: int,
           threads: int | None) -> tuple[float, int]:
    w, h = SIZES[size]
    sub = sub_of(tier_id)
    pf = PIX_FMT[sub]
    cfg = SimpleNamespace(
        width=w, height=h, fps=FPS, pix_fmt=pf, profile=tier_id, crf=None,
        has_alpha=False, entropy_mode="rans2", aq_mode="off", rdo_mode="off",
        gop="intra", color_metadata=ColorMetadataConfig(),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    enc = ToposVideoEncoder(str(out), cfg, slice_threads=threads,
                            slice_rows=None)
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
    return time.perf_counter() - t0, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="base")
    ap.add_argument("--size", default="2K", choices=list(SIZES))
    ap.add_argument("--tiers", nargs="+", default=["proxy"])
    ap.add_argument("--frames", type=int, default=FRAMES)
    ap.add_argument("--patch", action="append", default=[],
                    help="tier:field=value，可重复")
    ap.add_argument("--threads", type=int, default=None,
                    help="slice threads（默认产品自适应）")
    ap.add_argument("--crops", type=int, default=3)
    ap.add_argument("--no-panels", action="store_true")
    ap.add_argument("--features", type=int, default=None,
                    help="覆盖 ToposVideoEncoder._output_features 的返回值"
                         "（0 = 关输出特性，1 = 仅 DEBLOCK，3 = DEBLOCK+QPT2）")
    args = ap.parse_args()

    if args.features is not None:
        ToposVideoEncoder._output_features = lambda self: args.features
        print(f"[features] _output_features → {args.features}", flush=True)

    applied = apply_patches(args.patch)
    for line in applied:
        print(f"[patch] {line}", flush=True)

    w, h = SIZES[args.size]
    want = {i for i in SAMPLE_FRAMES if i < args.frames}
    refs: dict[str, dict] = {}
    codec = ToposCodec()
    results = []

    for tier_id in args.tiers:
        sub = sub_of(tier_id)
        pf = PIX_FMT[sub]
        if sub not in refs:
            refs[sub] = iter_master(pf, w, h, want)
        ref = refs[sub]

        out = OUT_DIR / f"{args.tag}_{args.size}_{tier_id}.mov"
        enc_s, n_enc = encode(args.size, tier_id, out, args.frames, args.threads)
        dec_s, dec_fps, video_bytes = decode_metrics(out, args.frames)

        tst = iter_topos(codec, out, want)
        q = quality(ref, tst)
        rq = rgb_quality(ref, tst) if sub == "422" else {}
        size_bytes = out.stat().st_size

        prores_path = MEDIA / PRORES_NAME[tier_id].replace(
            "2K_", f"{args.size}_", 1)
        prow = {}
        qrow = {}
        if prores_path.is_file() and tier_id in PRORES_NAME:
            pr = iter_prores(prores_path, pf, w, h, want)
            qrow = quality(ref, pr)
            if sub == "422":
                qrow.update(rgb_quality(ref, pr))
            pb = 0
            with av.open(str(prores_path)) as c:
                st = c.streams.video[0]
                seen = 0
                for p in c.demux(st):
                    if p.dts is None:
                        continue
                    pb += p.buffer_size
                    seen += 1
                    if seen >= args.frames:
                        break
            prow = {"file": prores_path.name, "bytes": prores_path.stat().st_size,
                    "video_bytes": pb,
                    "video_mbps": round(pb * 8 / (args.frames / 25.0) / 1e6, 2),
                    **qrow}

        row = {
            "tag": args.tag, "size": args.size, "tier": tier_id,
            "frames": args.frames, "threads": args.threads,
            "profile": tier_snapshot(tier_id),
            "file_bytes": size_bytes,
            "video_bytes": video_bytes,
            "video_mbps": round(video_bytes * 8 / (args.frames / 25.0) / 1e6, 2),
            "enc_fps": round(n_enc / enc_s, 1),
            "dec_fps": round(dec_fps, 1),
            "to_pos": False,
            **q, **rq,
        }
        if prow:
            row["prores"] = prow
            row["video_bytes_ratio"] = round(video_bytes / prow["video_bytes"], 4)
            row["file_bytes_ratio"] = round(size_bytes / prow["bytes"], 4)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=1), flush=True)

        if not args.no_panels:
            f0 = min(want)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            pr_f = (iter_prores(prores_path, pf, w, h, want)
                    if prores_path.is_file() else {})
            for j, (cy, cx) in enumerate(panel_ref_crops(ref, f0, args.crops)):
                if f0 in pr_f:
                    panel(ref[f0][0], pr_f[f0][0], tst[f0][0], cy, cx, 192, 5,
                          OUT_DIR / f"{args.tag}_{args.size}_{tier_id}_f{f0}_c{j}.png",
                          f"{args.size}/{tier_id}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "master": str(MASTER.relative_to(REPO)),
        "patch": applied,
        "reference": "母版 reformat（bicubic）→ YUV 4:2:2 10-bit limited",
        "note": "grid>1.2 = 误差在 8×8 块界（色块）；stair>0.6 = 残差轴向化（锯齿）；"
                "sharp<1 = 高频能量损失（颗粒掩蔽变弱）。",
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / f"{args.tag}_{args.size}_report.json"
    out_json.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"report → {out_json}")
    return 0


def panel_ref_crops(ref: dict, f0: int, k: int):
    from diagnose_edge_jaggies import auto_crops
    return auto_crops(ref[f0][0], k=k)


if __name__ == "__main__":
    raise SystemExit(main())
