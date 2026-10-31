"""P0：RD 对拍正式工具——codec_bench 成对文件 vs 各自编码输入 raw 的质量对拍。

口径（码率效率战役 G1/G2 门的评分器；计划表 §1 T0.1）：
- 素材家族（`bench_out/codec_bench/run_bench_tiers.py` 的 RAW 映射，两族不同源，
  只允许家族内对比）：
  - 422 家族：raw_{2K,4K}.yuv（yuv422p10le ← tests/test_videos/*K_prores422HQ.mov）
    ↔ prores proxy/LT/422/HQ ↔ topos proxy/LT/422/HQ；
  - 444 家族：raw_{2K,4K}444p{10,12}.yuv（yuv444p{10,12}le ← 4K_prores4444.mov，
    2K 为其 lanczos 降采样）↔ prores 4444/4444xq ↔ topos pro444/extreme；
- 指标：Y-PSNR（MAX=1023，12-bit 输出右移到 10-bit）+ SSIM-Y（每第 4 帧中心
  crop）、Mbps = 文件字节×8/(总帧数/25)；
- ProRes 走 PyAV（统一 reformat 到 yuv422p10le 取 Y）；topos 走产品解码路径
  （ToposMovieFile + ToposDecoder）；
- 输出：manifest 守护；JSON 证据 + 同码率插值（422 家族 ProRes 线）Δ 报告。

成对文件由 bench_out/codec_bench/run_bench_tiers.py 生成（媒体不入 git）；
缺失时本工具报错并提示生成命令，不代跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_rd_baseline import psnr, ssim_y  # noqa: E402

import av  # noqa: E402

FPS = 25.0
MAXV = 1023.0

RES = {
    "2k": dict(w=1920, h=1080, measure=96),
    "4k": dict(w=3840, h=2160, measure=48),
}

# (家族, 编解码方, 档位, 文件名模板)；域→raw 由 RAW 映射给出
FILES = [
    ("422", "prores", "proxy",   "prores_proxy_{res}.mov"),
    ("422", "prores", "LT",      "prores_lt_{res}.mov"),
    ("422", "prores", "422",     "prores_standard_{res}.mov"),
    ("422", "prores", "HQ",      "prores_hq_{res}.mov"),
    ("422", "topos",  "proxy",   "topos_proxy_{res}.mov"),
    ("422", "topos",  "LT",      "topos_lt_{res}.mov"),
    ("422", "topos",  "422",     "topos_standard_{res}.mov"),
    ("422", "topos",  "HQ",      "topos_hq_{res}.mov"),
    ("444", "prores", "4444",    "prores_4444_{res}.mov"),
    ("444", "prores", "4444XQ",  "prores_4444xq_{res}.mov"),
    ("444", "topos",  "4444",    "topos_pro444_{res}.mov"),
    ("444", "topos",  "4444XQ",  "topos_extreme_{res}.mov"),
]

RAW = {
    ("422", 10): "raw_{res}.yuv",
    ("444", 10): "raw_{res}444p10.yuv",
    ("444", 12): "raw_{res}444p12.yuv",
}
RES_NAME = {"2k": "2K", "4k": "4K"}


def raw_y_planes(bench: Path, res: str, domain: str, bd: int,
                 w: int, h: int, n: int):
    """编码输入 raw（mmap）→ 前 n 帧 Y 平面（12-bit 右移到 10-bit）。"""
    path = bench / RAW[(domain, bd)].format(res=RES_NAME[res])
    stride = (4 if domain == "422" else 6) * w * h // 2  # u16 元素/帧
    ysz = w * h
    mm = np.memmap(path, dtype="<u2", mode="r")
    frames_total = mm.size // stride
    out = []
    for i in range(min(n, frames_total)):
        y = np.asarray(mm[i * stride: i * stride + ysz]).reshape(h, w)
        if bd == 12:
            y = np.right_shift(y, 2).astype("<u2")
        out.append(y.copy())
    del mm
    return out, frames_total


def pyav_decode(path: Path, w: int, h: int, n: int):
    """ProRes → (Y 平面列表, 总帧数)。统一 reformat 到 yuv422p10le 取 Y。"""
    out = []
    total = 0
    with av.open(str(path)) as c:
        for frame in c.decode(video=0):
            total += 1
            if len(out) < n:
                if frame.format.name != "yuv422p10le":
                    frame = frame.reformat(format="yuv422p10le")
                plane = frame.planes[0]
                row = w * 2
                buf = memoryview(plane)
                if buf.nbytes == row * h:
                    y = np.frombuffer(buf, dtype="<u2").reshape(h, w)
                else:
                    y = np.empty((h, w), dtype="<u2")
                    for r in range(h):
                        y[r] = np.frombuffer(
                            buf[r * plane.line_size:r * plane.line_size + row],
                            dtype="<u2")
                out.append(np.ascontiguousarray(y))
    return out, total


def topos_decode(codec, path: Path, w: int, h: int, n: int):
    """topos MOV → (Y 平面列表, 总帧数, bit_depth)。产品解码路径。"""
    from src.shared.codec.topos_binding import ToposDecoder, ToposMovieFile

    mv = ToposMovieFile(codec, str(path))
    info = mv.info()
    bd = int(info.bit_depth)
    total = int(info.sample_count)
    try:
        first, size = mv.packet_into(0)
        dec = ToposDecoder(codec)
        di = dec.prepare(first, size)
        pc = int(di.plane_count)
        planes = []
        for p in range(pc):
            pw, ph = codec.plane_geometry(di, p)
            planes.append(np.empty((ph, pw), dtype="<u2"))
        views = dec.make_views(planes)
        out = []
        i = 0
        while i < total:
            b, s = (first, size) if i == 0 else mv.packet_into(i)
            i += 1
            if len(out) < n:
                dec.decode_views(b, views, pc, s)
                y = planes[0]
                if bd == 12:
                    y = np.right_shift(y, 2).astype("<u2")
                out.append(y.copy())
        dec.close()
        return out, total, bd
    finally:
        mv.close()


def interp_prores(pr_rows, mbps):
    """ProRes(422 家族) 率失真线在指定码率处的线性插值 Y-PSNR。"""
    if not pr_rows or mbps <= pr_rows[0]["mbps"] or mbps >= pr_rows[-1]["mbps"]:
        return None
    for k in range(len(pr_rows) - 1):
        a, b = pr_rows[k], pr_rows[k + 1]
        if a["mbps"] <= mbps <= b["mbps"]:
            t = (mbps - a["mbps"]) / (b["mbps"] - a["mbps"])
            return a["psnr_y"] + t * (b["psnr_y"] - a["psnr_y"])
    return None


def run_res(bench: Path, res: str, codec, manifest: dict):
    cfg = RES[res]
    w, h, n = cfg["w"], cfg["h"], cfg["measure"]
    print(f"===== {res} {w}x{h}（质量测量前 {n} 帧） =====", flush=True)
    refs, rows = {}, []
    for domain, fam, tier, pat in FILES:
        path = bench / pat.format(res=RES_NAME[res])
        if not path.exists():
            print(f"  [skip] 缺文件 {path.name}", flush=True)
            continue
        if domain not in refs:
            refs[domain] = raw_y_planes(bench, res, domain, 10, w, h, n)
        src, frames_total = refs[domain]
        mbps = path.stat().st_size * 8 / (frames_total / FPS) / 1e6
        if fam == "prores":
            ys, total, bd = (*pyav_decode(path, w, h, n), 10)
        else:
            ys, total, bd = topos_decode(codec, path, w, h, n)
        m = min(n, len(ys))
        ps = [psnr(src[i], ys[i], MAXV) for i in range(m)]
        ss = [ssim_y(src[i], ys[i], MAXV) for i in range(0, m, 4)]
        rows.append(dict(domain=domain, codec=fam, tier=tier, file=path.name,
                         mbps=round(mbps, 2), bit_depth=bd,
                         frames_total=total, frames_measured=m,
                         psnr_y=round(float(np.mean(ps)), 3),
                         ssim_y=round(float(np.mean(ss)), 5)))
        r = rows[-1]
        print(f"  [{domain}] {fam:6} {tier:8} {r['mbps']:8.1f} Mbps bd{bd} "
              f"Y {r['psnr_y']:6.2f} dB SSIM {r['ssim_y']:.5f}", flush=True)

    pr422 = sorted([r for r in rows if r["domain"] == "422" and
                    r["codec"] == "prores"], key=lambda r: r["mbps"])
    deltas = []
    for r in [x for x in rows if x["domain"] == "422" and x["codec"] == "topos"]:
        ip = interp_prores(pr422, r["mbps"])
        if ip is not None:
            deltas.append(dict(tier=r["tier"], mbps=r["mbps"],
                               psnr_y=r["psnr_y"], prores_interp=round(ip, 3),
                               delta_db=round(r["psnr_y"] - ip, 3)))
            print(f"  插值 topos {r['tier']:8} Δ {r['psnr_y'] - ip:+.2f} dB",
                  flush=True)
        else:
            deltas.append(dict(tier=r["tier"], mbps=r["mbps"],
                               psnr_y=r["psnr_y"], prores_interp=None,
                               delta_db=None))
    return dict(res=res, rows=rows, prores_interp_deltas=deltas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--res", nargs="+", default=["2k", "4k"],
                    choices=sorted(RES.keys()))
    ap.add_argument("--bench-dir", default=str(PROJECT_ROOT / "bench_out" / "codec_bench"))
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    bench = Path(args.bench_dir)
    missing = [p.format(res=RES_NAME[r]) for r in args.res
               for _, _, _, p in FILES if not (bench / p.format(res=RES_NAME[r])).exists()]
    if missing:
        print(f"缺成对文件：{missing}\n请先运行 bench_out/codec_bench/"
              f"run_bench_tiers.py 生成媒体（见该脚本头部）。",
              file=sys.stderr)
        return 2

    from src.shared.codec.topos_binding import ToposCodec
    codec = ToposCodec()
    codec.set_slice_threads(8)

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

    result = {"manifest": manifest, "cases": {
        res: run_res(bench, res, codec, manifest) for res in args.res}}
    (out_dir / "rd_vs_prores_result.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False))
    print(f"[result] {out_dir / 'rd_vs_prores_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
