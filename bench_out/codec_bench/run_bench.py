#!/usr/bin/env python3
"""Topos vs ProRes 422 HQ vs DNxHR HQX 编码/解码性能基准。

口径：
- 同一真实素材帧（tests/test_videos 的 ProRes 422 HQ 解出 yuv422p10le，
  2K/4K 各 N 帧，25fps）。
- 编码三方：
    topos-fixedqp : 单遍固定 qp（= 产品导出稳态路径，encode_frame）
    topos-sized   : 帧级目标码率搜索（= 档位定标/首帧路径，encode_sized，
                    目标取 hq 档 bpp ≈ ProRes 422 HQ）
    prores_hq     : ffmpeg prores_ks -profile:v 3（422 HQ）
    dnxhr_hqx     : ffmpeg dnxhd -profile:v dnxhr_hqx
- 解码三方（各自产物，含容器解封装）：
    topos   : ToposMovieFile（demux）+ ToposDecoder.decode_views（numpy 平面）
    prores  : ffmpeg -f null -（demux + 全解码）
    dnxhr   : 同上
- ffmpeg 全部默认线程（auto）；topos 用默认常驻池（min(ncpu,16)）。
- 每个 measurement 重复 RUNS 次取中位数。

用法：.venv/bin/python bench_out/codec_bench/run_bench.py [--frames 96] [--runs 3]
输出：bench_out/codec_bench/result.json + 控制台表格。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FFMPEG = "/tmp/ffmpeg-bin/ffmpeg"

# —— 被测几何（与 tests/test_videos 中的素材一致） ——
CASES = [
    {
        "name": "2K",
        "width": 1920,
        "height": 1080,
        "source": REPO / "tests/test_videos/2K_prores422HQ.mov",
    },
    {
        "name": "4K",
        "width": 3840,
        "height": 2160,
        "source": REPO / "tests/test_videos/4K_prores422HQ.mov",
    },
]


def sh(cmd: list, **kw) -> float:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return time.perf_counter() - t0


def median(values):
    return statistics.median(values)


def bench(name, fn, runs):
    times, extra = [], None
    for _ in range(runs):
        out = fn()
        times.append(out["seconds"])
        extra = out
    return {
        "name": name,
        "seconds_median": median(times),
        "seconds_all": [round(t, 4) for t in times],
        **{k: v for k, v in extra.items() if k != "seconds"},
    }


def extract_raw(case, n_frames, dst):
    """源 ProRes 422 HQ → yuv422p10le 原始帧序列（供三方编码器共用）。"""
    expect = n_frames * case["width"] * case["height"] * 2 * 2  # u16 × (Y+½U+½V)
    if dst.exists() and dst.stat().st_size == expect:
        return
    sh([
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-i", str(case["source"]),
        "-frames:v", str(n_frames),
        "-pix_fmt", "yuv422p10le",
        "-f", "rawvideo", str(dst),
    ])


def load_planes(raw, w, h, n):
    """按帧切 yuv422p10le → [ [Y, U, V] numpy '<u2' 视图 ]（零拷贝切片）。"""
    import numpy as np

    buf = np.fromfile(raw, dtype="<u2")
    ysz, csz = w * h, (w // 2) * h
    per = ysz + 2 * csz
    assert buf.size >= per * n, f"raw too small: {buf.size} < {per * n}"
    frames = []
    for i in range(n):
        off = i * per
        y = buf[off:off + ysz].reshape(h, w)
        u = buf[off + ysz:off + ysz + csz].reshape(h, w // 2)
        v = buf[off + ysz + csz:off + per].reshape(h, w // 2)
        frames.append([y, u, v])
    return frames


def frame_config(binding, w, h, qp):
    from src.shared.codec.topos_binding import TOPOS_CODEC_ABI_VERSION

    fc = binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(binding._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width, fc.visible_height = w, h
    fc.profile = 3          # Standard
    fc.pixel_format = 0     # YUV 4:2:2
    fc.bit_depth = 10
    fc.qmatrix_id = 1
    fc.qp_base = qp
    fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = fc.sar_den = 1
    return fc


def bench_topos_decode(codec, movie_path, w, h):
    """一轮：demux + 逐帧 decode_views（numpy 平面直写）。"""
    import numpy as np

    from src.shared.codec.topos_binding import ToposDecoder, ToposMovieFile

    def run():
        mv = ToposMovieFile(codec, str(movie_path))
        n = int(mv.info().sample_count)
        try:
            t0 = time.perf_counter()
            buf, size = mv.packet_into(0)
            with ToposDecoder(codec) as dec:
                info = dec.prepare(buf, size)
                pc = int(info.plane_count)
                planes = []
                for p in range(pc):
                    pw, ph = codec.plane_geometry(info, p)
                    planes.append(np.empty((ph, pw), dtype="<u2"))
                views = dec.make_views(planes)
                dec.decode_views(buf, views, pc, size)
                for i in range(1, n):
                    b, s = mv.packet_into(i)
                    dec.decode_views(b, views, pc, s)
            return {"seconds": time.perf_counter() - t0, "frames": n}
        finally:
            mv.close()

    return run


def bench_ffmpeg(cmd):
    def run():
        return {"seconds": sh(cmd)}

    return run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=96)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    import src.shared.codec.topos_binding as topos_binding
    from src.shared.codec.topos_binding import ToposCodec, ToposMuxFile
    from src.shared.codec.topos_profiles import get_topos_tier

    out_dir = Path(__file__).parent
    result = {"meta": {}, "cases": []}

    codec = ToposCodec()
    ver = codec.version()
    cpu = codec.cpu_features()
    threads = codec.slice_threads()
    result["meta"] = {
        "topos_version": f"{ver.major}.{ver.minor}.{ver.patch}",
        "topos_commit": ver.git_commit,
        "topos_slice_threads": threads,
        "topos_simd_avx2": cpu.avx2, "topos_simd_neon": cpu.neon,
        "cpu": subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True).stdout.strip(),
        "frames": args.frames, "runs": args.runs,
        "ffmpeg": subprocess.run([FFMPEG, "-version"], capture_output=True,
                                 text=True).stdout.splitlines()[0],
    }
    print("[meta]", json.dumps(result["meta"], ensure_ascii=False))

    tier = get_topos_tier("hq")

    for case in CASES:
        w, h = case["width"], case["height"]
        n = args.frames
        print(f"\n===== {case['name']} {w}x{h} × {n} frames =====")
        raw = out_dir / f"raw_{case['name']}.yuv"
        extract_raw(case, n, raw)
        frames = load_planes(raw, w, h, n)
        case_res = {"name": case["name"], "w": w, "h": h, "frames": n}

        # —— 编码 ——
        target = tier.frame_target_bytes(w, h)
        case_res["encode"] = []

        def topos_enc_run(sized):
            def run():
                fc = frame_config(topos_binding, w, h, qp=24)
                total = 0
                t0 = time.perf_counter()
                for planes in frames:
                    if sized is None:
                        pkt = codec.encode(fc, planes)
                    else:
                        pkt, _st, _qp = codec.encode_sized(fc, planes, sized)
                    total += len(pkt)
                return {"seconds": time.perf_counter() - t0, "bytes": total // n}
            return run

        for label, sized, runs in (
            ("topos-fixedqp(qp24)", None, args.runs),
            ("topos-sized(hq)", target, 1),
        ):
            b = bench(label, topos_enc_run(sized), runs)
            case_res["encode"].append(b)
            print(f"  encode {b['name']}: {b['seconds_median']/n*1000:.1f} ms/frame "
                  f"({n/b['seconds_median']:.1f} fps), "
                  f"{b['bytes']//1024} KiB/frame")

        # sized 产物 mux 成 MOV（解码基准 + 大小对比）
        topos_mov = out_dir / f"topos_{case['name']}.mov"
        if not topos_mov.exists():
            mcfg = codec.movie_config(w, h, qp=24)
            mcfg.profile, mcfg.pixel_format, mcfg.bit_depth = 3, 0, 10
            mcfg.qmatrix_id = 1
            fc = frame_config(topos_binding, w, h, qp=24)
            with ToposMuxFile(codec, str(topos_mov), mcfg) as mux:
                for i, planes in enumerate(frames):
                    pkt, _st, _qp = codec.encode_sized(fc, planes, target)
                    mux.add_packet(pkt, pts=i, dur=1)
                mux.finish()
        case_res["topos_mov_size"] = topos_mov.stat().st_size

        for tag, enc_args, dstname in (
            ("prores_hq(ks)",
             ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"],
             "prores_hq"),
            ("dnxhr_hqx",
             ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx"],
             "dnxhr_hqx"),
        ):
            dst = out_dir / f"{dstname}_{case['name']}.mov"
            if not dst.exists():
                sh([
                    FFMPEG, "-nostdin", "-v", "error", "-y",
                    "-f", "rawvideo", "-pix_fmt", "yuv422p10le",
                    "-s", f"{w}x{h}", "-r", "25", "-i", str(raw),
                    *enc_args, "-threads", "16",
                    "-f", "mov", str(dst),
                ])
            b = bench(tag, bench_ffmpeg([
                FFMPEG, "-nostdin", "-v", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "yuv422p10le",
                "-s", f"{w}x{h}", "-r", "25", "-i", str(raw),
                *enc_args, "-threads", "16",
                "-f", "mov", "/dev/null",
            ]), args.runs)
            b["bytes"] = dst.stat().st_size // n
            case_res["encode"].append(b)
            print(f"  encode {tag}: {b['seconds_median']/n*1000:.1f} ms/frame "
                  f"({n/b['seconds_median']:.1f} fps), {b['bytes']//1024} KiB/frame")

        # —— 解码（各自产物，含容器） ——
        case_res["decode"] = []
        b = bench("topos(demux+decode)",
                  bench_topos_decode(codec, topos_mov, w, h), args.runs)
        case_res["decode"].append(b)
        print(f"  decode topos: {b['seconds_median']/n*1000:.1f} ms/frame "
              f"({n/b['seconds_median']:.1f} fps)")

        for tag, path in (
            ("prores_hq(ks)", out_dir / f"prores_hq_{case['name']}.mov"),
            ("dnxhr_hqx", out_dir / f"dnxhr_hqx_{case['name']}.mov"),
        ):
            b = bench(tag, bench_ffmpeg([
                FFMPEG, "-nostdin", "-v", "error",
                "-i", str(path), "-threads", "16", "-f", "null", "-",
            ]), args.runs)
            b["frames"] = n
            case_res["decode"].append(b)
            print(f"  decode {tag}: {b['seconds_median']/n*1000:.1f} ms/frame "
                  f"({n/b['seconds_median']:.1f} fps)")

        result["cases"].append(case_res)

    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2))
    print("\n[result]", out_dir / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
