"""P2-01：RD 基线——无损源（源 YUV 平面直通）下的 QP × 熵模式（V1/V2 VLC）扫描。

口径（P2-02 决策数据源；§4 scoreboard 画质维度的证据）：
- 无损参考 = 源文件（ProRes/DNxHR）解码出的 planar YUV 平面本身（YUV 直通
  编码，无 RGB 转换噪声——量化损失全部来自 codec 本身）；
- 编码：`codec.encode` 直接 qp_base 阶梯（无码控干扰），V2 = reserved[0]=1
  （V2 + canonical VLC，major=2）；
- 指标：PSNR-Y/U/V（MAX=2^bd−1）、maxerr、SSIM-Y（scipy 全局高斯 SSIM，
  每第 4 帧中心 1920×1080 crop）、bytes/帧 + Mbps@源 fps、解码 p50/p95
  （ToposDecoder 产品路径）；
- 代际曲线：encode∘decode 反复 10 代（QP∈{30,45}×V1/V2），对 gen0 的
  Y-PSNR 与码率漂移；
- VMAF：静态 ffmpeg libvmaf 对 10/12-bit planar 输入的支持未验证，本工具
  暂不纳入（首门只要求 ΔY-PSNR/ΔY-SSIM）。

manifest：与主基准同一 `bench_manifest.build_manifest` 守护；
结果 `bench_out/<commit>/<dylib8>/<manifest8>/<utc>/rd_result.json` + rd_report.md。
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402

import av  # noqa: E402

TOPOS_CODEC_ABI_VERSION = 2
FPS_NOMINAL = 25.0

# 分辨率案例：profile 3=Standard(422)、5=Pro444(444 10/12)
CASES = {
    "2k": dict(src="tests/test_videos/2K_prores422HQ.mov", w=1920, h=1080,
               profile=3, pixel_format=0, bit_depth=10),
    "4k": dict(src="tests/test_videos/4K_prores4444.mov", w=3840, h=2160,
               profile=5, pixel_format=1, bit_depth=12),
}


def load_frames(path: Path, w: int, h: int, n: int, pf: int):
    """源 → planar '<u2' 帧列表 [(Y,U,V), …]（无损参考）。"""
    frames = []
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            planes = []
            for p in range(3):
                pw = w if p == 0 or pf == 1 else (w + 1) // 2
                ph = h
                plane = frame.planes[p]
                row_bytes = pw * 2
                buf = memoryview(plane)
                if buf.nbytes == row_bytes * ph:
                    arr = np.frombuffer(buf, dtype="<u2").reshape(ph, pw)
                else:  # 行尾对齐填充：逐行取
                    arr = np.empty((ph, pw), dtype="<u2")
                    for r in range(ph):
                        arr[r] = np.frombuffer(
                            buf[r * plane.line_size:r * plane.line_size + row_bytes],
                            dtype="<u2")
                planes.append(np.ascontiguousarray(arr))
            frames.append(planes)
            if len(frames) >= n:
                break
    return frames


def psnr(ref: np.ndarray, dec: np.ndarray, maxv: float) -> float:
    d = ref.astype(np.float64) - dec.astype(np.float64)
    mse = float(np.mean(d * d))
    if mse <= 0.0:
        return float("inf")
    return 10.0 * math.log10(maxv * maxv / mse)


def ssim_y(ref: np.ndarray, dec: np.ndarray, maxv: float,
           crop_h: int = 1080, crop_w: int = 1920) -> float:
    """全局高斯 SSIM（σ=1.5，K=0.01/0.03），中心 crop，float32。"""
    from scipy.ndimage import gaussian_filter

    h, w = ref.shape
    ch, cw = min(h, crop_h), min(w, crop_w)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    x = ref[y0:y0 + ch, x0:x0 + cw].astype(np.float32) / float(maxv)
    y = dec[y0:y0 + ch, x0:x0 + cw].astype(np.float32) / float(maxv)
    mu1 = gaussian_filter(x, 1.5)
    mu2 = gaussian_filter(y, 1.5)
    s11 = gaussian_filter(x * x, 1.5) - mu1 * mu1
    s22 = gaussian_filter(y * y, 1.5) - mu2 * mu2
    s12 = gaussian_filter(x * y, 1.5) - mu1 * mu2
    c1, c2 = 0.01 ** 2, 0.03 ** 2
    num = (2 * mu1 * mu2 + c1) * (2 * s12 + c2)
    den = (mu1 * mu1 + mu2 * mu2 + c1) * (s11 + s22 + c2)
    return float(np.mean(num / den))


def make_config(codec_lib, case, qp: int, entropy: int):
    from src.shared.codec.topos_binding import _CFrameConfig
    import ctypes
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = case["w"]
    fc.visible_height = case["h"]
    fc.profile = case["profile"]
    fc.pixel_format = case["pixel_format"]
    fc.bit_depth = case["bit_depth"]
    fc.qp_base = qp
    fc.qmatrix_id = 1
    fc.slice_rows = 16
    fc.color_range = 1
    fc.color_primaries = 1
    fc.color_transfer = 1
    fc.color_matrix = 1
    fc.reserved[0] = entropy  # 0=V1；1=V2+VLC（codec.c M9）
    return fc


def encode_frames(codec_lib, fc, ref_frames):
    pkts, total = [], 0
    for planes in ref_frames:
        pkt = codec_lib.encode(fc, list(planes))
        pkts.append(pkt)
        total += len(pkt)
    return pkts, total


class Decoder:
    """ToposDecoder 产品路径（预分配平面直写；含逐帧解码计时）。"""

    def __init__(self, codec_lib, first_pkt: bytes, case):
        from src.shared.codec.topos_binding import ToposDecoder
        self._dec = ToposDecoder(codec_lib)
        info = self._dec.prepare(first_pkt)
        self.pc = int(info.plane_count)
        self.planes = []
        for p in range(self.pc):
            pw, ph = codec_lib.plane_geometry(info, p)
            self.planes.append(np.empty((ph, pw), dtype="<u2"))
        self.views = self._dec.make_views(self.planes)

    def run(self, pkts):
        times = []
        for pkt in pkts:
            t0 = time.perf_counter()
            self._dec.decode_views(pkt, self.views, self.pc, len(pkt))
            times.append(time.perf_counter() - t0)
        return times

    def close(self):
        self._dec.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def rd_sweep(codec_lib, case, ref_frames, qps, ssim_every):
    rows = []
    maxv = float((1 << case["bit_depth"]) - 1)
    n = len(ref_frames)
    for entropy, tag in ((0, "V1"), (1, "V2")):
        for qp in qps:
            fc = make_config(codec_lib, case, qp, entropy)
            t0 = time.perf_counter()
            pkts, total = encode_frames(codec_lib, fc, ref_frames)
            enc_ms = (time.perf_counter() - t0) * 1000.0 / n
            with Decoder(codec_lib, pkts[0], case) as dec:
                # 指标必须在逐帧解码循环内即时计算——planes 是复用直写缓冲，
                # 循环外只剩最后一帧
                ps = [[], [], []]
                maxerr = 0
                ssims = []
                dt = []
                for i, pkt in enumerate(pkts):
                    t0 = time.perf_counter()
                    dec._dec.decode_views(pkt, dec.views, dec.pc, len(pkt))
                    dt.append(time.perf_counter() - t0)
                    for p in range(3):
                        ps[p].append(psnr(ref_frames[i][p], dec.planes[p], maxv))
                        maxerr = max(maxerr, int(np.max(np.abs(
                            ref_frames[i][p].astype(np.int32) -
                            dec.planes[p].astype(np.int32)))))
                    if i % ssim_every == 0:
                        ssims.append(ssim_y(ref_frames[i][0], dec.planes[0], maxv))
            row = dict(entropy=tag, qp=qp,
                       bytes_per_frame=total / n,
                       mbps=total * 8 * FPS_NOMINAL / n / 1e6,
                       enc_ms=round(enc_ms, 2),
                       dec_p50_ms=round(pct(dt, 0.5) * 1000, 3),
                       dec_p95_ms=round(pct(dt, 0.95) * 1000, 3),
                       psnr_y=round(float(np.mean(ps[0])), 3),
                       psnr_u=round(float(np.mean(ps[1])), 3),
                       psnr_v=round(float(np.mean(ps[2])), 3),
                       ssim_y=round(float(np.mean(ssims)), 5),
                       maxerr=maxerr)
            rows.append(row)
            print(f"  {tag} qp={qp:2d} {row['mbps']:7.2f} Mbps  "
                  f"Y {row['psnr_y']:6.2f} dB  SSIM {row['ssim_y']:.5f}  "
                  f"dec p95 {row['dec_p95_ms']:.2f} ms  err {maxerr}",
                  flush=True)
    return rows


def generation_curve(codec_lib, case, ref_frames, qps, generations=10):
    """encode∘decode 代际链：每代对 gen0 参考的 Y-PSNR + 码率漂移。"""
    maxv = float((1 << case["bit_depth"]) - 1)
    n = len(ref_frames)
    out = []
    for entropy, tag in ((0, "V1"), (1, "V2")):
        for qp in qps:
            cur = ref_frames
            row = dict(entropy=tag, qp=qp, gens=[])
            for g in range(1, generations + 1):
                fc = make_config(codec_lib, case, qp, entropy)
                pkts, total = encode_frames(codec_lib, fc, cur)
                decoded = [None] * n
                with Decoder(codec_lib, pkts[0], case) as dec:
                    for i, pkt in enumerate(pkts):
                        dec._dec.decode_views(pkt, dec.views, dec.pc, len(pkt))
                        decoded[i] = [p.copy() for p in dec.planes]
                cur = decoded
                ps = float(np.mean([
                    psnr(ref_frames[i][0], cur[i][0], maxv)
                    for i in range(n)]))
                row["gens"].append(dict(gen=g, psnr_y=round(ps, 3),
                                        bytes_pf=total / n))
            out.append(row)
            drift = (row["gens"][-1]["psnr_y"] - row["gens"][0]["psnr_y"])
            print(f"  GEN {tag} qp={qp}: gen1 {row['gens'][0]['psnr_y']:.3f} dB → "
                  f"gen{generations} {row['gens'][-1]['psnr_y']:.3f} dB "
                  f"(Δ {drift:+.3f})", flush=True)
    return out


def main() -> int:
    ap_args = sys.argv[1:]
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--res", nargs="+", default=["2k", "4k"],
                    choices=sorted(CASES.keys()))
    ap.add_argument("--frames", type=int, default=64)
    ap.add_argument("--qps", default="20,25,30,35,40,45,50,55")
    ap.add_argument("--gen-qps", default="30,45")
    ap.add_argument("--generations", type=int, default=10)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args(ap_args)

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    from src.shared.codec.topos_binding import ToposCodec
    codec_lib = ToposCodec()
    codec_lib.set_slice_threads(args.threads)
    manifest["threads_effective"] = args.threads

    import hashlib
    m8 = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (PROJECT_ROOT / "bench_out" / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / m8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)

    print("[manifest]", json.dumps(
        {k: manifest[k] for k in ("topos_version", "dylib_commit_embedded",
                                  "git_head", "git_dirty", "dylib_sha256")},
        ensure_ascii=False))
    qps = [int(q) for q in args.qps.split(",")]
    gen_qps = [int(q) for q in args.gen_qps.split(",")]
    ssim_every = 4

    result = {"manifest": manifest, "cases": {}, "generations": {}}
    for res in args.res:
        case = CASES[res]
        print(f"===== {res} {case['w']}x{case['h']} "
              f"pf={case['pixel_format']} bd={case['bit_depth']} =====",
              flush=True)
        frames = load_frames(PROJECT_ROOT / case["src"],
                             case["w"], case["h"], args.frames,
                             case["pixel_format"])
        print(f"  参考 {len(frames)} 帧（无损源平面直通）", flush=True)
        result["cases"][res] = dict(
            case={k: v for k, v in case.items() if k != "src"},
            frames=len(frames),
            rows=rd_sweep(codec_lib, case, frames, qps, ssim_every))
        print(f"  —— 代际曲线（qp {gen_qps}，{args.generations} 代，"
              f"{max(16, args.frames // 2)} 帧）——", flush=True)
        gen_frames = frames[:max(16, args.frames // 2)]
        result["generations"][res] = generation_curve(
            codec_lib, case, gen_frames, gen_qps, args.generations)

    (out_dir / "rd_result.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False))
    print(f"[result] {out_dir / 'rd_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
