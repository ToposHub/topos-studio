#!/usr/bin/env python3
"""W10/W11/W12：Windows 同机 TOPOS ↔ ProRes RD/画质/性能矩阵（PyAV，无 ffmpeg CLI）。

口径（对齐 docs/windows_topos_codec_sdk_optimization_plan_2026-09-28.md §4）：
- 同一原始母版、同帧区间（默认 32 帧）、同几何/位深/chroma；
- TOPOS 逐档 sized 编码（target = 档位定义 bpp）；ProRes prores_ks 同名档；
- 双轴：同名档配对 + 实测码率与 PSNR 逐点（供同码率插值）；
- PSNR 分通道（Y/U[V] 整数码值域，10/12-bit 不压位）；
- 多代漂移（1/3/5/10 代，HQ 与 4444 档）；
- 编码/解码墙钟（ms/frame）同机记录 —— W12 基线输入。

输出：native/topos_codec/docs/windows/win_rd_prores_win_2026-09-28.json
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

import av
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import src.shared.codec.topos_binding as binding  # noqa: E402
from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec, ToposDecoder, TOPOS_CODEC_ABI_VERSION)
from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS  # noqa: E402

TV = REPO / "tests" / "test_videos"
N_FRAMES = 32
PRORES_PROFILE_ID = {"proxy": 0, "lt": 1, "standard": 2, "hq": 3,
                     "4444": 4, "4444xq": 5}

# (case, tier_id, 母版) —— 444 家族用 4K 原生 12-bit 母版
MATRIX = [
    ("2K_422", "2K_prores422HQ.mov", ["proxy", "lt", "standard", "hq"]),
    ("4K_422", "4K_prores422HQ.mov", ["proxy", "lt", "standard", "hq"]),
    ("4K_444", "4K_prores4444.mov", ["4444", "4444xq"]),
]
# tier_id → (profile, pixel_format, bit_depth, prores 档名, 域)
TIER_CFG = {
    "proxy":    (3, 0, 10, "proxy", "422"),
    "lt":       (3, 0, 10, "lt", "422"),
    "standard": (3, 0, 10, "standard", "422"),
    "hq":       (3, 0, 10, "hq", "422"),
    "4444":     (5, 1, 10, "4444", "444"),
    "4444xq":   (6, 1, 12, "4444xq", "444"),
}


def _plane_widths(fmt_name: str, w: int) -> list:
    if "422" in fmt_name:
        return [w, w // 2, w // 2]
    return [w, w, w]  # 444/GBR


def _frame_planes(fr) -> list:
    """从 av.VideoFrame 按 plane 缓冲逐行提取（to_ndarray 会把 422 上采样
    成 444 —— 左半≠真实色度，RD 实验不可用）。"""
    fmt = fr.format.name
    h = fr.height
    ws = _plane_widths(fmt, fr.width)
    out = []
    for plane, pw in zip(fr.planes, ws):
        stride = plane.line_size
        raw = bytes(plane)
        rows = [raw[r * stride:r * stride + pw * 2] for r in range(h)]
        out.append(np.frombuffer(b"".join(rows), dtype="<u2").reshape(h, pw).copy())
    return out


def _frame_from_planes(planes, fmt: str, w: int, h: int):
    """av.VideoFrame 手工填充（from_ndarray 不接受 422 平面布局；
    plane.update 需要紧致 pw*2 × h 字节，实测逐 plane 精确匹配）。"""
    fr = av.VideoFrame(w, h, fmt)
    for plane, arr in zip(fr.planes, planes):
        plane.update(np.ascontiguousarray(arr, dtype="<u2").tobytes())
    return fr


def decode_master(path: Path, n: int):
    """取母版前 n 帧 → [(y,u,v) uint16]（保真原 pixel format）。"""
    frames = []
    with av.open(str(path)) as c:
        st = c.streams.video[0]
        for i, fr in enumerate(c.decode(st)):
            if i >= n:
                break
            frames.append(_frame_planes(fr))
    return frames


def psnr_ch(ref: np.ndarray, test: np.ndarray, bd: int) -> float:
    d = ref.astype(np.float64) - test.astype(np.float64)
    mse = float(np.mean(d * d))
    peak = float((1 << bd) - 1)
    if mse == 0.0:
        return 999.0
    return 20.0 * np.log10(peak) - 10.0 * np.log10(mse)


def topos_encode_frames(codec, frames, profile, pf, bd, target):
    fc = binding._CFrameConfig()
    fc.struct_size = ctypes.sizeof(binding._CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = frames[0][0].shape[1]
    fc.visible_height = frames[0][0].shape[0]
    fc.profile = profile
    fc.pixel_format = pf
    fc.bit_depth = bd
    fc.qmatrix_id = 1
    fc.qp_base = 24
    fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = fc.sar_den = 1
    t0 = time.perf_counter()
    pkts = [codec.encode_sized(fc, f, target)[0] for f in frames]
    dt = time.perf_counter() - t0
    return pkts, dt / len(frames)


def topos_decode_all(codec, pkts):
    with ToposDecoder(codec) as dec:
        info = dec.prepare(pkts[0], len(pkts[0]))
        pc = int(info.plane_count)
        planes = []
        for p in range(pc):
            pw, ph = codec.plane_geometry(info, p)
            planes.append(np.empty((ph, pw), dtype="<u2"))
        views = dec.make_views(planes)
        t0 = time.perf_counter()
        outs = []
        for pkt in pkts:
            dec.decode_views(pkt, views, pc, len(pkt))
            outs.append([p.copy() for p in planes])
        dt = time.perf_counter() - t0
    return outs, dt / len(pkts)


def prores_encode(frames, prores_profile, pf_name, w, h):
    out = av.open("nul" if sys.platform == "win32" else "/dev/null", "w",
                  format="mov" if sys.platform != "win32" else "mov")
    st = out.add_stream("prores_ks", rate=25)
    st.pix_fmt = pf_name
    st.width, st.height = w, h
    # PyAV 的 .profile 属性对 prores_ks 被静默忽略；options 才生效
    st.codec_context.options = {"profile": prores_profile}
    sizes = 0
    t0 = time.perf_counter()
    for f in frames:
        fr = _frame_from_planes(f, pf_name, w, h)
        fr.pts = None
        for pkt in st.encode(fr):
            sizes += pkt.size
    for pkt in st.encode(None):
        sizes += pkt.size
    dt = time.perf_counter() - t0
    out.close()
    return sizes, dt / len(frames)


def prores_decode_sizes_profile(frames, prores_profile, pf_name, w, h, tmp: Path):
    """写出临时 mov（供解码与码率核算）→ 解码返回帧。"""
    with av.open(str(tmp), "w", format="mov") as out:
        st = out.add_stream("prores_ks", rate=25)
        st.pix_fmt = pf_name
        st.width, st.height = w, h
        st.codec_context.options = {"profile": prores_profile}
        for i, f in enumerate(frames):
            fr = _frame_from_planes(f, pf_name, w, h)
            fr.pts = i
            for pkt in st.encode(fr):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    dec = []
    with av.open(str(tmp)) as c:
        st = c.streams.video[0]
        for fr in c.decode(st):
            dec.append(_frame_planes(fr))
    return dec


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", default=None,
                    help="只跑指定 case（可多次）")
    args = ap.parse_args()
    codec = ToposCodec()
    codec.set_slice_threads(8)
    result = {"meta": {
        "frames": N_FRAMES, "machine": "win-msvc 16C24T",
        "note": "同一母版同帧区间；PSNR 为整数码值域分通道；码率为包字节均值",
    }, "cases": {}}
    tmp = REPO / "bench_out" / "win_rd_tmp.mov"
    tmp.parent.mkdir(exist_ok=True)

    for case_name, master, tiers in MATRIX:
        if args.case and case_name not in args.case:
            continue
        frames = decode_master(TV / master, N_FRAMES)
        h, w = frames[0][0].shape
        cw = frames[0][1].shape[1]
        result["cases"][case_name] = {
            "master": master, "w": w, "h": h, "chroma_w": cw,
            "planes": "422" if cw == w // 2 else "444", "tiers": {},
        }
        print(f"\n===== {case_name} ← {master} ({w}x{h}, {len(frames)}f) =====")
        for tier_id in tiers:
            profile, pf, bd, prores_name, domain = TIER_CFG[tier_id]
            pf_name = f"yuv{'422' if domain == '422' else '444'}p{bd}le"
            tier_def = TOPOS_PROFILE_TIERS[tier_id]
            target = tier_def.frame_target_bytes(w, h)

            # 仅 444 家族的 12-bit 母版喂 bd10 档需要 >>2 下变换（参考=同一
            # 10-bit 版，对齐 run_bench_tiers 的 -pix_fmt yuv444p10le 口径）；
            # 422 母版本身是 yuv422p10le，不得再移位
            down = 2 if (domain == "444" and bd == 10) else 0
            src_frames = frames if down == 0 else [
                [pl >> down for pl in f] for f in frames]

            pkts, enc_ms = topos_encode_frames(codec, frames if bd >= 12 else src_frames,
                                               profile, pf, bd, target)
            outs, dec_ms = topos_decode_all(codec, pkts)
            t_psnr = [psnr_ch(src_frames[i][p], outs[i][p], bd)
                      for p in range(3) for i in range(len(frames))]
            t_bytes = sum(len(p) for p in pkts) / len(pkts)

            if domain == "444":
                # ffmpeg prores_ks 的 444 家族只收 p10 输入且恒出 12-bit
                # （10-bit 码 <<2 嵌入）：仅 bd10 档有对应规格；bd12 记 N/A
                if bd >= 12:
                    row_note = ("prores_ks 444/4444xq 不接受 yuv444p12le 输入"
                                "（ffmpeg 实现域），无对应规格 → N/A")
                    result["cases"][case_name]["tiers"][tier_id] = {
                        "target_bytes": target,
                        "topos": {"bytes": round(sum(len(p) for p in pkts) / len(pkts)),
                                  "bpp": round(sum(len(p) for p in pkts) / len(pkts) * 8 / (w * h), 3),
                                  "psnr_yuv": [round(float(np.mean(
                                      [psnr_ch(src_frames[i][pn], outs[i][pn], bd)
                                       for i in range(len(frames))])), 2) for pn in range(3)],
                                  "enc_ms": round(enc_ms * 1000, 1),
                                  "dec_ms": round(dec_ms * 1000, 1)},
                        "prores": {"na": row_note}}
                    print(f"  {tier_id:9s} topos {result['cases'][case_name]['tiers'][tier_id]['topos']['bpp']:6.3f}bpp"
                          f" PSNR {result['cases'][case_name]['tiers'][tier_id]['topos']['psnr_yuv']}"
                          f" | prores N/A（{row_note[:18]}…）")
                    continue
                p_frames = prores_decode_sizes_profile(
                    src_frames, prores_name, "yuv444p10le", w, h, tmp)
                p_frames = [[pl >> 2 for pl in f] for f in p_frames]  # 12-bit 容器还原
            else:
                p_frames = prores_decode_sizes_profile(
                    src_frames, prores_name, pf_name, w, h, tmp)
            p_bytes = tmp.stat().st_size / len(frames)  # mov 容器口径（含容器开销）
            p_psnr = [psnr_ch(src_frames[i][p], p_frames[i][p], bd)
                      for p in range(3) for i in range(len(frames))]

            def agg(vals):
                n = len(frames)
                return [round(float(np.mean(vals[i * n:(i + 1) * n])), 2)
                        for i in range(3)]

            row = {
                "target_bytes": target,
                "topos": {"bytes": round(t_bytes), "bpp": round(t_bytes * 8 / (w * h), 3),
                          "psnr_yuv": agg(t_psnr),
                          "enc_ms": round(enc_ms * 1000, 1),
                          "dec_ms": round(dec_ms * 1000, 1)},
                "prores": {"bytes": round(p_bytes), "bpp": round(p_bytes * 8 / (w * h), 3),
                           "psnr_yuv": agg(p_psnr),
                           "container": "mov"},
            }
            result["cases"][case_name]["tiers"][tier_id] = row
            print(f"  {tier_id:9s} topos {row['topos']['bpp']:6.3f}bpp "
                  f"PSNR Y/U/V {row['topos']['psnr_yuv']} "
                  f"enc {row['topos']['enc_ms']}ms | prores {prores_name} "
                  f"{row['prores']['bpp']:6.3f}bpp PSNR {row['prores']['psnr_yuv']}")

    # —— W11：多代漂移（HQ @2K_422；4444 @4K_444）——
    gens_report = {}
    for case_name, tier_id in (("2K_422", "hq"), ("4K_444", "4444")):
        case = result["cases"].get(case_name)
        if case is None:
            continue  # --case 过滤掉的组合
        master = case["master"]
        profile, pf, bd, _, _ = TIER_CFG[tier_id]
        frames = decode_master(TV / master, min(N_FRAMES, 8))
        h, w = frames[0][0].shape
        target = TOPOS_PROFILE_TIERS[tier_id].frame_target_bytes(w, h)
        # 与主矩阵同口径：仅 444 家族 bd10 档对 12-bit 母版下变换
        down = 2 if (case["planes"] == "444" and bd == 10) else 0
        cur = frames if down == 0 else [[pl >> down for pl in f] for f in frames]
        ref = cur  # 下变换后的源（参考与编码输入同域）
        drift = []
        for gen in (1, 3, 5, 10):
            pkts, _ = topos_encode_frames(codec, cur, profile, pf, bd, target)
            cur, _ = topos_decode_all(codec, pkts)
            ps = [psnr_ch(ref[i][p], cur[i][p], bd)
                  for p in range(3) for i in range(len(ref))]
            n = len(frames)
            drift.append({"gen": gen, "psnr_yuv": [round(float(np.mean(ps[pn * n:(pn + 1) * n])), 2)
                                                   for pn in range(3)]})
        gens_report[f"{case_name}/{tier_id}"] = drift
        print(f"  gens {case_name}/{tier_id}: {drift}")
    result["multigen"] = gens_report

    out = REPO / "native/topos_codec/docs/windows/win_rd_prores_win_2026-09-28.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.unlink(missing_ok=True)
    print(f"[result] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
