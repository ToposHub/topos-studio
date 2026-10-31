#!/usr/bin/env python3
"""Topos 编码 vs ProRes 422 HQ / DNxHR HQX 统一对比基准（P0-02/P0-03 重写版）。

相对旧版的契约修复（审计 P0-02/P0-03 + 2026-09-03 务实建议）：

1. **生命周期对称**：双口径，两侧同口径才可比较，禁止跨口径相除——
   - ``core``：topos = 裸 `encode_frame` 循环（无 open/mux/I/O，平面内存
     物化）；ffmpeg = 页缓存 raw 读取 → `-f null`（无 mux）。core 列
     的 ffmpeg 侧含 OS 读缓冲成本，对 topos 偏保守，结论以 E2E 为准。
   - ``e2e``：topos = 产品 `ToposVideoEncoder` open+逐帧+close（含 mux
     与落盘）；ffmpeg = 子进程 raw 文件 → mov 文件。两侧完整生命周期。
2. **交替 A/B + 中位数 + bootstrap CI**：`--repeats` 同时作用于两侧；
   逐轮交替（topos→ref），禁止 best-of；相对结论给出配对比值中位数与
   95% CI，CI 跨 1.0 的行只允许写"相当/无法区分"。
3. **产物位深自动探测（P0-03）**：每个 E2E 产物 ffprobe 记录
   codec/profile/pix_fmt；DNxHR 标签一律带实际位深（"DNxHR HQX (10-bit
   output)"），不同位深不得进同一张比较表。
4. **manifest 断言（P0-04）**：开工前 `bench_manifest.build_manifest()`
   校验 dylib commit == 工作树 HEAD（脏树需 --allow-dirty 并降档），
   结果写入不可覆盖目录
   ``bench_out/<commit>/<dylib_sha8>/<manifest_sha8>/<utc>/``。
5. **线程固定并记录**：topos 常驻池与 ffmpeg `-threads` 同值，写入
   manifest。

质量（PSNR/VMAF）不在本工具——P2-01 要求无损源与同码率插值，混在
速度工具里只会产出误导性数字。

用法：
    .venv/bin/python tools/bench_topos_vs_dnxhr_prores.py \
        [--res 2k|4k|both] [--frames 96] [--repeats 5] [--threads 16] \
        [--tier hq] [--allow-dirty]
输出：<result_dir>/result.json + 控制台表格
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
for p in (str(PROJECT_ROOT), str(TOOLS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402

FPS = 25

SOURCES = {
    "2k": ("tests/test_videos/2K_prores422HQ.mov", 1920, 1080),
    "4k": ("tests/test_videos/4K_prores422HQ.mov", 3840, 2160),
}

REFS = [
    {"name": "prores_hq",
     "label": "ProRes 422 HQ",
     "enc": ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"]},
    {"name": "dnxhr_hqx",
     "label": "DNxHR HQX (10-bit output)",  # P0-03：标签带实际位深
     "enc": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx"]},
]


def resolve_ffmpeg() -> tuple:
    """(ffmpeg, ffprobe) 探测：env > imageio-ffmpeg > static-ffmpeg > PATH。"""
    env = os.environ.get("TOPOS_BENCH_FFMPEG")
    if env:
        p = Path(env)
        return str(p), str(p.with_name("ffprobe"))
    try:
        import imageio_ffmpeg
        f = Path(imageio_ffmpeg.get_ffmpeg_exe())
        return str(f), str(f.with_name(f.stem.replace("ffmpeg", "ffprobe") + f.suffix))
    except Exception:
        pass
    try:
        import static_ffmpeg.run as sfr
        return sfr.get_or_fetch_platform_executables_else_raise()
    except Exception:
        pass
    import shutil
    f = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if f and fp:
        return f, fp
    raise RuntimeError("未找到 ffmpeg/ffprobe（pip install static-ffmpeg 或 "
                       "设置 TOPOS_BENCH_FFMPEG）")


FFMPEG, FFPROBE = resolve_ffmpeg()


def sh(cmd: list) -> float:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr[-1500:]}")
    return time.perf_counter() - t0


def ffprobe_stream(path: str) -> dict:
    r = subprocess.run([
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile,pix_fmt,width,height,nb_frames",
        "-of", "json", path,
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr[-500:]}")
    streams = json.loads(r.stdout).get("streams", [])
    return streams[0] if streams else {}


# ———————————————————————————— 素材 ————————————————————————————

def extract_raw(source: str, w: int, h: int, n: int, dst: Path) -> Path:
    expect = n * w * h * 2 * 2  # yuv422p10le: u16 × (Y + ½U + ½V)
    if dst.exists() and dst.stat().st_size == expect:
        return dst
    sh([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", source,
        "-frames:v", str(n), "-pix_fmt", "yuv422p10le",
        "-f", "rawvideo", str(dst)])
    assert dst.stat().st_size == expect, f"{dst} 尺寸不符"
    return dst


def load_frames(raw: Path, w: int, h: int, n: int):
    import numpy as np
    buf = np.fromfile(raw, dtype="<u2")
    ysz, csz = w * h, (w // 2) * h
    per = ysz + 2 * csz
    frames = []
    for i in range(n):
        off = i * per
        frames.append((
            buf[off:off + ysz].reshape(h, w),
            buf[off + ysz:off + ysz + csz].reshape(h, w // 2),
            buf[off + ysz + csz:off + per].reshape(h, w // 2),
        ))
    return frames


# ———————————————————————————— topos 两侧 ————————————————————————————

def topos_core_run(codec, fc, frames) -> float:
    """core 口径：裸 encode_frame 循环（无 mux/无 open/无 I/O）。"""
    t0 = time.perf_counter()
    for planes in frames:
        codec.encode(fc, list(planes))
    return time.perf_counter() - t0


class BenchFrame:
    """ToposVideoEncoder.encode_frame 的最小 duck-type 输入（planar 直通）。"""

    def __init__(self, planes, pix_fmt: str, w: int, h: int, index: int) -> None:
        self.planes = planes
        self.is_planar = True
        self.pixel_format = pix_fmt
        self.width = w
        self.height = h
        self.frame_index = index


def topos_e2e_run(tier_id, frames, w, h, out_path) -> float:
    """E2E 口径：产品编码器 open + 逐帧 + close（mux/落盘/平面校验全含）。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder

    cfg = SimpleNamespace(
        width=w, height=h, fps=FPS, pix_fmt="yuv422p10le",
        profile=tier_id, crf=None, has_alpha=False,
    )
    enc = ToposVideoEncoder(out_path, cfg)
    bframes = [BenchFrame(p, "yuv422p10le", w, h, i)
               for i, p in enumerate(frames)]  # 物化在计时之外
    t0 = time.perf_counter()
    enc.open()
    for f in bframes:
        enc.encode_frame(f)
    enc.close()
    return time.perf_counter() - t0


def topos_decode_run(codec, mov: Path) -> float:
    """解码口径：ToposMovieFile（demux）+ ToposDecoder 逐帧直写 numpy。"""
    from src.shared.codec.topos_binding import ToposDecoder, ToposMovieFile

    mv = ToposMovieFile(codec, str(mov))
    n = int(mv.info().sample_count)
    try:
        t0 = time.perf_counter()
        buf, size = mv.packet_into(0)
        with ToposDecoder(codec) as dec:
            info = dec.prepare(buf, size)
            pc = int(info.plane_count)
            import numpy as np
            planes = []
            for p in range(pc):
                pw, ph = codec.plane_geometry(info, p)
                planes.append(np.empty((ph, pw), dtype="<u2"))
            views = dec.make_views(planes)
            dec.decode_views(buf, views, pc, size)
            for i in range(1, n):
                b, s = mv.packet_into(i)
                dec.decode_views(b, views, pc, s)
        return time.perf_counter() - t0
    finally:
        mv.close()


# ———————————————————————————— ffmpeg 参照 ————————————————————————————

def ffmpeg_core_run(enc_args, raw, w, h, n) -> float:
    """core 口径（ffmpeg 侧）：页缓存 raw → -f null（无 mux；无产物落盘）。"""
    return sh([FFMPEG, "-nostdin", "-v", "error",
               "-f", "rawvideo", "-pix_fmt", "yuv422p10le",
               "-s", f"{w}x{h}", "-r", str(FPS), "-i", str(raw),
               *enc_args, "-threads", "16", "-f", "null", "-"])


def ffmpeg_e2e_run(enc_args, raw, w, h, out_path) -> float:
    """E2E 口径（ffmpeg 侧）：子进程 raw 文件 → mov 文件（含 open/finalize）。"""
    return sh([FFMPEG, "-nostdin", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "yuv422p10le",
               "-s", f"{w}x{h}", "-r", str(FPS), "-i", str(raw),
               *enc_args, "-threads", "16", "-f", "mov", str(out_path)])


# ———————————————————————————— 统计 ————————————————————————————

def median_ci(samples, iters: int = 4000, alpha: float = 0.05,
              rng: random.Random | None = None):
    """中位数 + bootstrap 95% CI。样本 < 2 时 CI 为 None。"""
    vals = sorted(samples)
    med = statistics.median(vals)
    if len(vals) < 2:
        return med, None
    rng = rng or random.Random(0xC0FFEE)
    n = len(vals)
    means = []
    for _ in range(iters):
        means.append(statistics.mean(
            vals[rng.randrange(n)] for _ in range(n)))
    means.sort()
    lo = means[max(0, int((alpha / 2) * iters))]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters) - 1)]
    return med, (lo, hi)


def paired_ratio_ci(pairs, iters: int = 4000, rng: random.Random | None = None):
    """配对比值（topos/ref）中位数 + bootstrap CI；CI 跨 1.0 = 无法区分。"""
    ratios = [t / r for t, r in pairs if r > 0]
    med, ci = median_ci(ratios, iters=iters, rng=rng)
    return med, ci


def _interpret_ratio(rci) -> str:
    """ratio = topos耗时/ref耗时：<1 = topos 更快；CI 跨 1.0 = 无法区分。"""
    if rci is None:
        return "indistinguishable"
    if rci[1] < 1.0:
        return "topos faster"
    if rci[0] > 1.0:
        return "ref faster"
    return "indistinguishable"


def fmt_ci(ci) -> str:
    return f"[{ci[0]:.2f},{ci[1]:.2f}]" if ci else "n/a"


# ———————————————————————————— 主流程 ————————————————————————————

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", choices=["2k", "4k", "both"], default="both")
    ap.add_argument("--frames", type=int, default=96)
    ap.add_argument("--repeats", type=int, default=5,
                    help="交替 A/B 轮数（同时作用于两侧）")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--tier", default="hq")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="脏工作树也测量（结果只能归档为历史观察）")
    args = ap.parse_args()

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    from src.shared.codec.topos_binding import ToposCodec
    codec_lib = ToposCodec()
    codec_lib.set_slice_threads(args.threads)
    manifest["threads_effective"] = args.threads

    # 不可覆盖结果目录（计划 §0 规则 3）
    import hashlib
    m8 = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (PROJECT_ROOT / "bench_out" / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / m8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)

    res_list = ["2k", "4k"] if args.res == "both" else [args.res]
    rng = random.Random(0xBADC0DE)
    result = {"manifest": manifest, "cases": []}

    print("[manifest]", json.dumps(
        {k: manifest[k] for k in ("topos_version", "dylib_commit_embedded",
                                  "git_head", "git_dirty", "dylib_sha256")},
        ensure_ascii=False))
    print(f"[ffmpeg] {FFMPEG}")
    print(f"[scope]  repeats={args.repeats}（交替 A/B 双侧）, "
          f"threads={args.threads}, frames={args.frames}")

    from src.shared.codec.topos_profiles import get_topos_tier

    for res in res_list:
        source, w, h = SOURCES[res]
        n = args.frames
        raw = extract_raw(str(PROJECT_ROOT / source), w, h, n,
                          out_dir / f"raw_{res}.yuv")
        frames = load_frames(raw, w, h, n)
        tier = get_topos_tier(args.tier)
        case = {"res": res, "w": w, "h": h, "frames": n, "tier": args.tier,
                "encode": [], "decode": []}
        print(f"\n===== {res} {w}x{h} × {n} =====")

        # —— 编码：core 口径（交替 A/B） ——
        from src.shared.codec import topos_binding as tb_mod
        fc_cfg = _frame_config(tb_mod, w, h)
        pairs_core = {}
        for rep in range(args.repeats + 1):  # 首轮 warmup，不计入
            t = topos_core_run(codec_lib, fc_cfg, frames)
            for ref in REFS:
                fr = ffmpeg_core_run(ref["enc"], raw, w, h, n)
                if rep > 0:
                    pairs_core.setdefault(ref["name"], []).append((t, fr))
        for ref in REFS:
            pr = pairs_core[ref["name"]]
            t_med, t_ci = median_ci([p[0] for p in pr], rng=rng)
            f_med, f_ci = median_ci([p[1] for p in pr], rng=rng)
            ratio, rci = paired_ratio_ci(pr, rng=rng)
            case["encode"].append({
                "scope": "core", "ref": ref["name"],
                "topos_s_median": t_med, "topos_ci95": t_ci,
                "ref_s_median": f_med, "ref_ci95": f_ci,
                "ratio_median": ratio, "ratio_ci95": rci,
                "interpretation": _interpret_ratio(rci),
            })
            print(f"  ENC core  {ref['label']:28s} topos {t_med/n*1000:7.1f} ms/f "
                  f"| ref {f_med/n*1000:7.1f} ms/f | ratio {ratio:.2f} "
                  f"CI{fmt_ci(rci)} → {case['encode'][-1]['interpretation']}")

        # —— 编码：E2E 口径（交替 A/B）+ 产物 ffprobe（P0-03） ——
        pairs_e2e = {}
        probes = {}
        topos_mov = out_dir / f"topos_{args.tier}_{res}.mov"
        ref_movs = {r["name"]: out_dir / f"ref_{r['name']}_{res}.mov"
                    for r in REFS}
        for rep in range(args.repeats + 1):
            t = topos_e2e_run(args.tier, frames, w, h, str(topos_mov))
            for ref in REFS:
                fr = ffmpeg_e2e_run(ref["enc"], raw, w, h, str(ref_movs[ref["name"]]))
                if rep > 0:
                    pairs_e2e.setdefault(ref["name"], []).append((t, fr))
        probes["topos"] = ffprobe_stream(str(topos_mov))
        for ref in REFS:
            probes[ref["name"]] = ffprobe_stream(str(ref_movs[ref["name"]]))
            pr = pairs_e2e[ref["name"]]
            t_med, t_ci = median_ci([p[0] for p in pr], rng=rng)
            f_med, f_ci = median_ci([p[1] for p in pr], rng=rng)
            ratio, rci = paired_ratio_ci(pr, rng=rng)
            case["encode"].append({
                "scope": "e2e", "ref": ref["name"],
                "topos_s_median": t_med, "topos_ci95": t_ci,
                "ref_s_median": f_med, "ref_ci95": f_ci,
                "ratio_median": ratio, "ratio_ci95": rci,
                "topos_file_bytes": topos_mov.stat().st_size,
                "ref_file_bytes": ref_movs[ref["name"]].stat().st_size,
                "topos_probe": probes["topos"],
                "ref_probe": probes[ref["name"]],
                "interpretation": _interpret_ratio(rci),
            })
            print(f"  ENC e2e   {ref['label']:28s} topos {t_med/n*1000:7.1f} ms/f "
                  f"| ref {f_med/n*1000:7.1f} ms/f | ratio {ratio:.2f} "
                  f"CI{fmt_ci(rci)} → {case['encode'][-1]['interpretation']}")
            topos_pf = probes['topos'].get('pix_fmt') or \
                f"tpic:{probes['topos'].get('codec_name')}(ffprobe-unknown)"
            print(f"            topos pix_fmt={topos_pf} | "
                  f"ref pix_fmt={probes[ref['name']].get('pix_fmt')} "
                  f"profile={probes[ref['name']].get('profile')}")

        # —— 解码（各自 E2E 产物，配对交替） ——
        pairs_dec = {}
        for rep in range(args.repeats + 1):
            t = topos_decode_run(codec_lib, topos_mov)
            for ref in REFS:
                fr = sh([FFMPEG, "-nostdin", "-v", "error", "-i",
                         str(ref_movs[ref["name"]]), "-threads", "16",
                         "-f", "null", "-"])
                if rep > 0:
                    pairs_dec.setdefault(ref["name"], []).append((t, fr))
        for ref in REFS:
            pr = pairs_dec[ref["name"]]
            t_med, _ = median_ci([p[0] for p in pr], rng=rng)
            f_med, _ = median_ci([p[1] for p in pr], rng=rng)
            ratio, rci = paired_ratio_ci(pr, rng=rng)
            case["decode"].append({
                "ref": ref["name"],
                "topos_s_median": t_med, "ref_s_median": f_med,
                "ratio_median": ratio, "ratio_ci95": rci,
                "interpretation": _interpret_ratio(rci),
            })
            print(f"  DEC       {ref['label']:28s} topos {t_med/n*1000:7.1f} ms/f "
                  f"| ref {f_med/n*1000:7.1f} ms/f | ratio {ratio:.2f} "
                  f"CI{fmt_ci(rci)} → {case['decode'][-1]['interpretation']}")

        result["cases"].append(case)

    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[result] {out_dir / 'result.json'}")
    return 0


def _frame_config(tb_mod, w, h):
    """core 口径的 FrameConfig（422p10，qp24 起点）。"""
    import ctypes
    from src.shared.codec.topos_binding import TOPOS_CODEC_ABI_VERSION

    fc = tb_mod._CFrameConfig()
    fc.struct_size = ctypes.sizeof(tb_mod._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width, fc.visible_height = w, h
    fc.profile = 3
    fc.pixel_format = 0
    fc.bit_depth = 10
    fc.qmatrix_id = 1
    fc.qp_base = 24
    fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = fc.sar_den = 1
    return fc


if __name__ == "__main__":
    sys.exit(main())
