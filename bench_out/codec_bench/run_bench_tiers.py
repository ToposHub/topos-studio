#!/usr/bin/env python3
"""Topos 六档位全量基准（编码 sized + 解码，2K/4K），逐档配 ProRes 参照。

档位 → 配置（src/shared/codec/topos_profiles.py 单一真相源）：
    proxy    profile3 yuv422p10  target ≈ 5.5 MB/s@1080p25  ↔ prores_ks proxy
    lt       profile3 yuv422p10  11.0                        ↔ prores_ks lt
    standard profile3 yuv422p10  16.5                        ↔ prores_ks standard
    hq       profile3 yuv422p10  24.0                        ↔ prores_ks hq
    pro444   profile5 yuv444p10  37.0                        ↔ prores_ks 4444
    extreme  profile6 yuv444p12  56.5                        ↔ prores_ks 4444xq

422 档素材：tests/test_videos/*_prores422HQ.mov（yuv422p10le）；
444 档素材：4K_prores4444.mov（yuv444p12le，2K 为其 lanczos 降采样）。
口径与 run_bench.py 相同（96 帧 × 3 次中位数，topos 池 16 线程，ffmpeg -threads 16）。

用法：.venv/bin/python bench_out/codec_bench/run_bench_tiers.py [--frames 96] [--runs 3]
输出：bench_out/codec_bench/tiers_result.json + 控制台表格。
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

CASES = [
    {"name": "2K", "width": 1920, "height": 1080},
    {"name": "4K", "width": 3840, "height": 2160},
]

# (domain, bit_depth) → 每 case 的 raw 文件名与来源
RAW_SOURCES = {
    ("422", 10): {
        "2K": ("raw_2K.yuv", "tests/test_videos/2K_prores422HQ.mov", []),
        "4K": ("raw_4K.yuv", "tests/test_videos/4K_prores422HQ.mov", []),
    },
    ("444", 10): {
        "2K": ("raw_2K444p10.yuv", "tests/test_videos/4K_prores4444.mov",
               ["-vf", "scale=1920:1080:flags=lanczos"]),
        "4K": ("raw_4K444p10.yuv", "tests/test_videos/4K_prores4444.mov", []),
    },
    ("444", 12): {
        "2K": ("raw_2K444p12.yuv", "tests/test_videos/4K_prores4444.mov",
               ["-vf", "scale=1920:1080:flags=lanczos"]),
        "4K": ("raw_4K444p12.yuv", "tests/test_videos/4K_prores4444.mov", []),
    },
}

TIERS = [
    {"id": "proxy",    "label": "topos 422 Proxy", "profile": 3, "pf": 0, "bd": 10,
     "domain": "422", "prores": "proxy"},
    {"id": "lt",       "label": "topos 422 LT",    "profile": 3, "pf": 0, "bd": 10,
     "domain": "422", "prores": "lt"},
    {"id": "standard", "label": "topos 422",       "profile": 3, "pf": 0, "bd": 10,
     "domain": "422", "prores": "standard"},
    {"id": "hq",       "label": "topos 422 HQ",    "profile": 3, "pf": 0, "bd": 10,
     "domain": "422", "prores": "hq"},
    {"id": "pro444",   "label": "topos 4444",      "profile": 5, "pf": 1, "bd": 10,
     "domain": "444", "prores": "4444"},
    {"id": "extreme",  "label": "topos 4444 XQ",   "profile": 6, "pf": 1, "bd": 12,
     "domain": "444", "prores": "4444xq"},
]


def sh(cmd: list) -> float:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return time.perf_counter() - t0


def median(values):
    return statistics.median(values)


def extract_raw(case, domain, bd, n, out_dir):
    name, src, vf = RAW_SOURCES[(domain, bd)][case["name"]]
    dst = out_dir / name
    planes = 3
    expect = n * case["width"] * case["height"] * 2 * planes  # u16 × (Y+U+V 或 Y+½U+½V)
    if domain == "422":
        expect = n * case["width"] * case["height"] * 2 * 2
    if dst.exists() and dst.stat().st_size == expect:
        return dst
    sh([
        FFMPEG, "-nostdin", "-v", "error", "-y",
        "-i", str(REPO / src), "-frames:v", str(n), *vf,
        "-pix_fmt", f"yuv{'422' if domain == '422' else '444'}p{bd}le",
        "-f", "rawvideo", str(dst),
    ])
    assert dst.stat().st_size == expect, f"{dst}: {dst.stat().st_size} != {expect}"
    return dst


def load_planes(raw, w, h, n, domain):
    import numpy as np

    buf = np.fromfile(raw, dtype="<u2")
    ysz = w * h
    cw = w // 2 if domain == "422" else w
    csz = cw * h
    per = ysz + 2 * csz
    frames = []
    for i in range(n):
        off = i * per
        y = buf[off:off + ysz].reshape(h, w)
        u = buf[off + ysz:off + ysz + csz].reshape(h, cw)
        v = buf[off + ysz + csz:off + per].reshape(h, cw)
        frames.append([y, u, v])
    return frames


def frame_config(binding, w, h, profile, pf, bd):
    from src.shared.codec.topos_binding import TOPOS_CODEC_ABI_VERSION

    fc = binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(binding._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width, fc.visible_height = w, h
    fc.profile = profile
    fc.pixel_format = pf
    fc.bit_depth = bd
    fc.qmatrix_id = 1
    fc.qp_base = 24
    fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = fc.sar_den = 1
    return fc


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=96)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    n = args.frames

    import src.shared.codec.topos_binding as topos_binding
    from src.shared.codec.topos_binding import ToposCodec, ToposDecoder, ToposMovieFile, ToposMuxFile
    from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS

    out_dir = Path(__file__).parent
    result = {"meta": {}, "tiers": []}
    codec = ToposCodec()
    result["meta"] = {
        "topos_version": "0.1.0",
        "topos_slice_threads": codec.slice_threads(),
        "frames": n, "runs": args.runs,
    }

    for tier in TIERS:
        tier_def = TOPOS_PROFILE_TIERS[tier["id"]]
        entry = {"id": tier["id"], "label": tier["label"], "cases": []}
        print(f"\n===== {tier['label']} (profile{tier['profile']} "
              f"{'422' if tier['domain'] == '422' else '444'}p{tier['bd']}, "
              f"target {tier_def.target_bpp:.2f} bpp) =====")

        for case in CASES:
            w, h = case["width"], case["height"]
            raw = extract_raw(case, tier["domain"], tier["bd"], n, out_dir)
            frames = load_planes(raw, w, h, n, tier["domain"])
            target = tier_def.frame_target_bytes(w, h)
            case_res = {"name": case["name"], "w": w, "h": h}

            # —— topos sized 编码基准 ——
            def sized_run():
                fc = frame_config(topos_binding, w, h, tier["profile"],
                                  tier["pf"], tier["bd"])
                total, qp0 = 0, None
                t0 = time.perf_counter()
                for i, planes in enumerate(frames):
                    pkt, _st, qp = codec.encode_sized(fc, planes, target)
                    if i == 0:
                        qp0 = qp
                    total += len(pkt)
                return {"seconds": time.perf_counter() - t0,
                        "bytes": total // n, "qp_frame0": qp0}

            enc = bench(f"topos-sized", sized_run, args.runs)
            case_res["encode_topos"] = enc
            print(f"  {case['name']} ENC topos: {enc['seconds_median']/n*1000:7.1f} ms/frame "
                  f"({n/enc['seconds_median']:6.1f} fps), "
                  f"{enc['bytes']*8/w/h:5.2f} bpp, qp0={enc['qp_frame0']}")

            # mux 产物（解码基准用；单遍 sized 写出）
            topos_mov = out_dir / f"topos_{tier['id']}_{case['name']}.mov"
            if not topos_mov.exists():
                mcfg = codec.movie_config(w, h, qp=24, profile=tier["profile"],
                                          bit_depth=tier["bd"],
                                          pixel_format=tier["pf"])
                fc = frame_config(topos_binding, w, h, tier["profile"],
                                  tier["pf"], tier["bd"])
                with ToposMuxFile(codec, str(topos_mov), mcfg) as mux:
                    for i, planes in enumerate(frames):
                        pkt, _st, _qp = codec.encode_sized(fc, planes, target)
                        mux.add_packet(pkt, pts=i, dur=1)
                    mux.finish()

            # —— topos 解码基准 ——
            def decode_run():
                mv = ToposMovieFile(codec, str(topos_mov))
                cnt = int(mv.info().sample_count)
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
                        for i in range(1, cnt):
                            b, s = mv.packet_into(i)
                            dec.decode_views(b, views, pc, s)
                    return {"seconds": time.perf_counter() - t0}
                finally:
                    mv.close()

            dec = bench("topos", decode_run, args.runs)
            case_res["decode_topos"] = dec
            print(f"  {case['name']} DEC topos: {dec['seconds_median']/n*1000:7.1f} ms/frame "
                  f"({n/dec['seconds_median']:6.1f} fps)")

            # —— ProRes 参照（逐档同 profile） ——
            pf_name = f"yuv{'422' if tier['domain'] == '422' else '444'}p{tier['bd']}le"
            ref_mov = out_dir / f"prores_{tier['prores']}_{case['name']}.mov"
            if not ref_mov.exists():
                sh([
                    FFMPEG, "-nostdin", "-v", "error", "-y",
                    "-f", "rawvideo", "-pix_fmt", pf_name,
                    "-s", f"{w}x{h}", "-r", "25", "-i", str(raw),
                    "-c:v", "prores_ks", "-profile:v", tier["prores"],
                    "-threads", "16",
                    "-f", "mov", str(ref_mov),
                ])

            def prores_enc_run():
                return {"seconds": sh([
                    FFMPEG, "-nostdin", "-v", "error", "-y",
                    "-f", "rawvideo", "-pix_fmt", pf_name,
                    "-s", f"{w}x{h}", "-r", "25", "-i", str(raw),
                    "-c:v", "prores_ks", "-profile:v", tier["prores"],
                    "-threads", "16",
                    "-f", "mov", "/dev/null",
                ])}

            penc = bench(f"prores_{tier['prores']}", prores_enc_run, args.runs)
            penc["bytes"] = ref_mov.stat().st_size // n
            case_res["encode_prores"] = penc
            print(f"  {case['name']} ENC prores({tier['prores']}): "
                  f"{penc['seconds_median']/n*1000:7.1f} ms/frame "
                  f"({n/penc['seconds_median']:6.1f} fps), "
                  f"{penc['bytes']*8/w/h:5.2f} bpp")

            def prores_dec_run():
                return {"seconds": sh([
                    FFMPEG, "-nostdin", "-v", "error",
                    "-i", str(ref_mov), "-threads", "16", "-f", "null", "-",
                ])}

            pdec = bench(f"prores_{tier['prores']}", prores_dec_run, args.runs)
            case_res["decode_prores"] = pdec
            print(f"  {case['name']} DEC prores({tier['prores']}): "
                  f"{pdec['seconds_median']/n*1000:7.1f} ms/frame "
                  f"({n/pdec['seconds_median']:6.1f} fps)")

            entry["cases"].append(case_res)
        result["tiers"].append(entry)

    (out_dir / "tiers_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2))
    print("\n[result]", out_dir / "tiers_result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
