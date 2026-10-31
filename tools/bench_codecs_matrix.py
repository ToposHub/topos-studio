"""对照组基准：Topos 全档位 vs ProRes vs DNxHR——速度/码率/画质三方矩阵。

口径（对照组工具，2026-09-03）：
- 素材家族（与 bench_out/codec_bench/run_bench_tiers.py 相同，家族内才可对比）：
  - 422：raw_{2K,4K}.yuv（yuv422p10le）→ topos proxy/lt/standard/hq
    ↔ prores_ks proxy/lt/standard/hq ↔ DNxHR HQX（10-bit 422 唯一档）；
    另附 DNxHR LB/SQ/HQ（8-bit yuv422p）参照行——ffmpeg 中 LB/SQ/HQ 仅收
    8-bit，位深不同不进同一 Δ 比较，仅作码率/速度参照；
  - 44410：raw_{res}444p10.yuv → topos pro444 ↔ prores_ks 4444 ↔ DNxHR 444；
  - 44412：raw_{res}444p12.yuv → topos extreme ↔ prores_ks 4444xq
    （DNxHR 上限 10-bit，无 12-bit 对应，跳过并注明）。
- 速度：
  - topos = 绑定循环（encode_sized 逐帧确定性搜索 / ToposMovieFile+ToposDecoder
    解码循环），池 16 线程，3 次取中位（与旧 tiers 基准同口径）；
  - ProRes/DNxHR = PyAV 8.1.2 进程内 container 路线（add_stream options
    传 profile；本 PyAV 的 CodecContext.profile setter 为空操作，实测无效，
    必须走 options 字典）：编码计时写 /dev/null（含 mux，含容器 open/close），
    解码计时走产品文件 demux+decode 循环，thread_count=16。
    注：/tmp ffmpeg CLI 已不可用，参照系由 CLI 改为 PyAV 进程内——勿与旧
    CLI 证据逐行直接相除。
- 码率：产品文件字节 ×8/(帧数/25)；画质：Y-PSNR（MAX=1023，12-bit 右移）
  + SSIM-Y（每第 4 帧），参考 = 各家族编码输入 raw（bench_rd_vs_prores 同源）。
- topos 编码带 tier 声明的 chroma_qp_offset（pro444/extreme=4，6d749586 起
  产品编码器行为）；topos/DNxHR 产物全部重生成，ProRes 统一由本机 PyAV 重产。
- manifest 守护；证据落
  bench_out/<commit>/<dylib8>/<manifest8>/<utc>/codecs_matrix.json(+.md)。

用法：
    .venv/bin/python tools/bench_codecs_matrix.py [--res both|2k|4k] \
        [--families 422,44410,44412] [--tiers proxy,...] \
        [--refs prores:proxy,dnxhr:hqx,...] [--runs 3] [--allow-dirty]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import numpy as np
import av

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_rd_baseline import psnr, ssim_y  # noqa: E402
from bench_rd_vs_prores import (  # noqa: E402
    pyav_decode, raw_y_planes, topos_decode,
)

FPS = 25.0
MAXV = 1023.0
BENCH = PROJECT_ROOT / "bench_out" / "codec_bench"

RES = {
    "2k": dict(w=1920, h=1080, measure=96),
    "4k": dict(w=3840, h=2160, measure=48),
}
RES_NAME = {"2k": "2K", "4k": "4K"}

# 编码器 priv AVOption 的 profile 常量名（options 字典专用，见模块 docstring）
PRORES_OPT = {"proxy": "proxy", "lt": "lt", "standard": "standard",
              "hq": "hq", "4444": "4444", "4444xq": "4444xq"}
DNX_OPT = {"lb": "dnxhr_lb", "sq": "dnxhr_sq", "hq": "dnxhr_hq",
           "hqx": "dnxhr_hqx", "444": "dnxhr_444"}

# 家族定义：topos 条目 = (tier_id, profile, pixel_format, bit_depth)；
# dnxhr 条目 = (档名, pix_fmt, bit_depth)；dnxhr_supp = 8-bit 附加参照行。
FAMILIES = {
    "422": dict(
        domain="422", bd=10, raw="raw_{res}.yuv", pix_fmt="yuv422p10le",
        topos=[("proxy", 3, 0, 10), ("lt", 3, 0, 10),
               ("standard", 3, 0, 10), ("hq", 3, 0, 10)],
        prores=["proxy", "lt", "standard", "hq"],
        prores_supp=[],
        dnxhr=[("hqx", "yuv422p10le", 10)],
        dnxhr_supp=[("lb", "yuv422p", 8), ("sq", "yuv422p", 8),
                    ("hq", "yuv422p", 8)],
        pairs=[("proxy", "proxy", None), ("lt", "lt", None),
               ("standard", "standard", None), ("hq", "hq", "hqx")],
    ),
    "44410": dict(
        domain="444", bd=10, raw="raw_{res}444p10.yuv", pix_fmt="yuv444p10le",
        topos=[("pro444", 5, 1, 10)],
        prores=["4444"],
        prores_supp=[],
        dnxhr=[("444", "yuv444p10le", 10)],
        dnxhr_supp=[],
        pairs=[("pro444", "4444", "444")],
    ),
    "44412": dict(
        domain="444", bd=12, raw="raw_{res}444p12.yuv", pix_fmt="yuv444p12le",
        topos=[("extreme", 6, 1, 12)],
        prores=[],
        # 本 PyAV 的 prores_ks 仅支持 10-bit（video_formats 实测无
        # yuv444p12le），XQ 以 444p10 深度受限附加行测量；12-bit XQ 质量
        # 引用 6d749586 rd 证据（CLI 8.0 产物）在报告中对照。
        prores_supp=[("4444xq", "yuv444p10le", 10)],
        dnxhr=[],
        dnxhr_supp=[],
        pairs=[("extreme", "4444xq", None)],
    ),
}


def median(v):
    return statistics.median(v)


RES_NAME_CUR = ["2K"]  # 产品命名用（run 内更新）


# ———————————————————————————— topos 侧 ————————————————————————————

def topos_frame_config(binding, w, h, profile, pf, bd, chroma_off,
                       entropy_v2=True):
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
    fc.qp_delta_chroma = chroma_off
    # T1.5 转正：默认 V2 canonical VLC（本工具对拍基线；产品默认已转 rans2，见 ADR-C038）；--entropy v1 对拍
    fc.reserved[0] = 1 if entropy_v2 else 0
    fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = fc.sar_den = 1
    return fc


def topos_encode_cell(codec, binding, frames, w, h, tier_id, tier_def,
                      tier_cfg, runs, entropy_v2=True):
    """sized 编码计时 + 产品写出（含 tier chroma_qp_offset + 熵模式）。"""
    profile, pf, bd = tier_cfg[1], tier_cfg[2], tier_cfg[3]
    chroma_off = int(getattr(tier_def, "chroma_qp_offset", 0) or 0)
    target = tier_def.frame_target_bytes(w, h)

    def sized_run():
        fc = topos_frame_config(binding, w, h, profile, pf, bd, chroma_off,
                                entropy_v2)
        t0 = time.perf_counter()
        for planes in frames:
            pkt, _st, _qp = codec.encode_sized(fc, planes, target)
        return {"seconds": time.perf_counter() - t0}

    enc_s = sorted(sized_run()["seconds"] for _ in range(runs))

    product = BENCH / f"topos_{tier_id}_{RES_NAME_CUR[0]}.mov"
    if product.exists():
        product.unlink()
    mcfg = codec.movie_config(w, h, qp=24, profile=profile,
                              bit_depth=bd, pixel_format=pf)
    from src.shared.codec.topos_binding import ToposMuxFile
    fc = topos_frame_config(binding, w, h, profile, pf, bd, chroma_off,
                            entropy_v2)
    t0 = time.perf_counter()
    with ToposMuxFile(codec, str(product), mcfg) as mux:
        for i, planes in enumerate(frames):
            pkt, _st, _qp = codec.encode_sized(fc, planes, target)
            mux.add_packet(pkt, pts=i, dur=1)
        mux.finish()
    prod_s = time.perf_counter() - t0
    return dict(enc_s=enc_s, enc_median=median(enc_s), product=str(product),
                product_write_s=round(prod_s, 3), chroma_qp_offset=chroma_off,
                target_bytes_frame=target)


def topos_decode_cell(codec, product, runs):
    from src.shared.codec.topos_binding import ToposDecoder, ToposMovieFile

    def decode_run():
        mv = ToposMovieFile(codec, str(product))
        cnt = int(mv.info().sample_count)
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
                for i in range(1, cnt):
                    b, s = mv.packet_into(i)
                    dec.decode_views(b, views, pc, s)
            return {"seconds": time.perf_counter() - t0, "frames": cnt}
        finally:
            mv.close()

    dec_s = []
    for _ in range(runs):
        out = decode_run()
        dec_s.append(out["seconds"] / out["frames"])
    dec_s.sort()
    return dec_s


def load_np_frames(raw: Path, w, h, n, domain):
    mm = np.memmap(raw, dtype="<u2", mode="r")
    cw = w // 2 if domain == "422" else w
    ysz, csz = w * h, cw * h
    per = ysz + 2 * csz
    frames = []
    for i in range(n):
        off = i * per
        frames.append([
            np.ascontiguousarray(mm[off:off + ysz]).reshape(h, w),
            np.ascontiguousarray(mm[off + ysz:off + ysz + csz]).reshape(h, cw),
            np.ascontiguousarray(mm[off + ysz + csz:off + per]).reshape(h, cw),
        ])
    del mm
    return frames


# ———————————————————————————— PyAV 参照侧 ————————————————————————————

def build_av_frames(raw: Path, w, h, n, pix_fmt, out_bd, raw_bd):
    """raw yuv（u16）→ av.VideoFrame 列表（一次性物化，计时外）。

    out_bd < raw_bd 时右移量化（如 12→10、10→8，确定性无抖动）。
    """
    sub422 = pix_fmt in ("yuv422p10le", "yuv422p")
    mm = np.memmap(raw, dtype="<u2", mode="r")
    cw = w // 2 if sub422 else w
    ysz, csz = w * h, cw * h
    per = ysz + 2 * csz
    dt = "<u2" if out_bd >= 10 else "u1"
    shift = max(0, raw_bd - out_bd)
    item = 2 if out_bd >= 10 else 1
    frames = []
    for i in range(n):
        base = i * per
        f = av.VideoFrame(w, h, pix_fmt)
        arrays = (
            mm[base:base + ysz].reshape(h, w),
            mm[base + ysz:base + ysz + csz].reshape(h, cw),
            mm[base + ysz + csz:base + per].reshape(h, cw),
        )
        for plane, arr in zip(f.planes, arrays):
            if shift:
                arr = np.right_shift(arr, shift)
            arr = np.ascontiguousarray(arr, dtype=dt)
            tight = arr.shape[1] * item
            if plane.line_size == tight:
                plane.update(arr.tobytes())
            else:  # 行有填充：铺到 line_size 再整体写入
                pad = np.zeros((arr.shape[0],
                                plane.line_size // item), dtype=dt)
                pad[:, :arr.shape[1]] = arr
                plane.update(pad.tobytes())
        f.time_base = Fraction(1, 25)
        frames.append(f)
    del mm
    return frames


def ref_encode_cell(codec_name, profile_opt, w, h, pix_fmt, frames, runs,
                    threads, product: Path):
    """container 路线编码：计时写 /dev/null（含 mux），产品写真实文件。

    profile 经 add_stream(options={'profile': …}) 传入（本 PyAV 的
    CodecContext.profile setter 无效，见模块 docstring）。
    """

    def encode_to(path: str) -> float:
        t0 = time.perf_counter()
        with av.open(path, "w", format="mov") as out:
            st = out.add_stream(codec_name, rate=25,
                                options={"profile": profile_opt})
            cc = st.codec_context
            cc.width, cc.height, cc.pix_fmt = w, h, pix_fmt
            cc.thread_count = threads
            for i, f in enumerate(frames):
                f.pts = i
                for pkt in st.encode(f):
                    out.mux(pkt)
            for pkt in st.encode(None):
                out.mux(pkt)
        return time.perf_counter() - t0

    enc_s = sorted(encode_to("/dev/null") for _ in range(runs))
    prod_s = encode_to(str(product))

    probe = {}
    with av.open(str(product)) as c:
        st0 = c.streams.video[0]
        cc0 = st0.codec_context
        probe = dict(codec=cc0.name or codec_name, pix_fmt=cc0.pix_fmt,
                     profile=str(st0.profile), width=cc0.width,
                     height=cc0.height, frames=int(st0.frames or 0))
    return dict(enc_s=enc_s, enc_median=median(enc_s), product=str(product),
                product_write_s=round(prod_s, 3), probe=probe,
                profile_opt=profile_opt)


def ref_decode_cell(product, runs, threads):
    def dec_run():
        with av.open(str(product)) as c:
            st = c.streams.video[0]
            st.codec_context.thread_count = threads
            t0 = time.perf_counter()
            cnt = 0
            for _frame in c.decode(st):
                cnt += 1
            return (time.perf_counter() - t0) / max(1, cnt)

    return sorted(dec_run() for _ in range(runs))


# ———————————————————————————— 画质 ————————————————————————————

def quality_metrics(codec, path, codec_kind, src_planes, w, h, measure):
    if codec_kind == "topos":
        ys, _total, _bd = topos_decode(codec, Path(path), w, h, measure)
    else:
        ys, _total = pyav_decode(Path(path), w, h, measure)
    m = min(measure, len(ys))
    ps = [psnr(src_planes[i], ys[i], MAXV) for i in range(m)]
    ss = [ssim_y(src_planes[i], ys[i], MAXV) for i in range(0, m, 4)]
    return dict(psnr_y=round(float(np.mean(ps)), 3),
                ssim_y=round(float(np.mean(ss)), 5), frames_measured=m)


def interp_line(points, mbps):
    """RD 线（按 mbps 升序）在 mbps 处的线性插值 PSNR；端点外为 None。"""
    if len(points) < 2 or mbps <= points[0]["mbps"] or \
            mbps >= points[-1]["mbps"]:
        return None
    for k in range(len(points) - 1):
        a, b = points[k], points[k + 1]
        if a["mbps"] <= mbps <= b["mbps"]:
            t = (mbps - a["mbps"]) / (b["mbps"] - a["mbps"])
            return a["psnr_y"] + t * (b["psnr_y"] - a["psnr_y"])
    return None


# ———————————————————————————— 主流程 ————————————————————————————

def run_family(res, fam_name, codec, binding, args, threads):
    fam = FAMILIES[fam_name]
    w, h, n = RES[res]["w"], RES[res]["h"], args.frames
    measure = RES[res]["measure"]
    raw = BENCH / fam["raw"].format(res=RES_NAME[res])
    if not raw.exists():
        print(f"  [skip] 缺 raw {raw.name}")
        return None
    # 422 每帧 4·w·h 字节（Y + 2×半宽色度，u16）；444 每帧 6·w·h 字节
    frames_total = raw.stat().st_size // (
        2 * (2 if fam["domain"] == "422" else 3) * w * h)
    src_planes, _ = raw_y_planes(BENCH, res, fam["domain"], fam["bd"],
                                 w, h, measure)
    cells = []

    def want_ref(kind, key):
        return not args.refs or f"{kind}:{key}" in args.refs

    # —— topos 档 ——
    from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS
    wanted = set(args.tiers) if args.tiers else None
    for tier_cfg in fam["topos"]:
        tier_id = tier_cfg[0]
        if wanted and tier_id not in wanted:
            continue
        tier_def = TOPOS_PROFILE_TIERS[tier_id]
        frames = load_np_frames(raw, w, h, n, fam["domain"])
        enc = topos_encode_cell(codec, binding, frames, w, h, tier_id,
                                tier_def, tier_cfg, args.runs,
                                args.entropy == "v2")
        del frames
        dec = topos_decode_cell(codec, enc["product"], args.runs)
        mbps = Path(enc["product"]).stat().st_size * 8 / (frames_total / FPS) / 1e6
        q = quality_metrics(codec, enc["product"], "topos", src_planes,
                            w, h, measure)
        cell = dict(res=res, family=fam_name, codec="topos", variant=tier_id,
                    bit_depth=fam["bd"], supplementary=False,
                    entropy=args.entropy,
                    mbps=round(mbps, 2),
                    bpp=round(mbps * 1e6 / (25 * w * h), 3),
                    enc_ms=round(enc["enc_median"] / n * 1000, 2),
                    enc_fps=round(n / enc["enc_median"], 1),
                    dec_ms=round(median(dec) * 1000, 2),
                    dec_fps=round(1 / median(dec), 1),
                    chroma_qp_offset=enc["chroma_qp_offset"],
                    enc_s=[round(x, 3) for x in enc["enc_s"]],
                    dec_s=[round(x, 4) for x in dec], **q)
        cells.append(cell)
        print(f"  [topos ] {tier_id:9} {cell['mbps']:8.1f} Mbps "
              f"Y {cell['psnr_y']:6.2f} dB SSIM {cell['ssim_y']:.5f} | "
              f"enc {cell['enc_ms']:7.1f} ms/f dec {cell['dec_ms']:6.1f} ms/f",
              flush=True)

    # —— ProRes 参照（PyAV container 路线；主行 + 深度受限附加行） ——
    pro_entries = [(p, fam["pix_fmt"], fam["bd"], False)
                   for p in fam["prores"]]
    pro_entries += [(p, pf, bd, True)
                    for p, pf, bd in fam.get("prores_supp", [])]
    for prof, pf, bd, supp in pro_entries:
        if not want_ref("prores", prof):
            continue
        product = BENCH / f"prores_{prof}_{RES_NAME_CUR[0]}.mov"
        if product.exists():
            product.unlink()
        frames = build_av_frames(raw, w, h, n, pf, bd, fam["bd"])
        enc = ref_encode_cell("prores_ks", PRORES_OPT[prof], w, h,
                              pf, frames, args.runs, threads, product)
        del frames
        dec = ref_decode_cell(enc["product"], args.runs, threads)
        mbps = Path(enc["product"]).stat().st_size * 8 / (frames_total / FPS) / 1e6
        q = quality_metrics(codec, enc["product"], "prores", src_planes,
                            w, h, measure)
        cell = dict(res=res, family=fam_name, codec="prores", variant=prof,
                    bit_depth=bd, supplementary=supp,
                    mbps=round(mbps, 2),
                    bpp=round(mbps * 1e6 / (25 * w * h), 3),
                    enc_ms=round(enc["enc_median"] / n * 1000, 2),
                    enc_fps=round(n / enc["enc_median"], 1),
                    dec_ms=round(median(dec) * 1000, 2),
                    dec_fps=round(1 / median(dec), 1),
                    probe=enc["probe"], profile_opt=enc["profile_opt"],
                    enc_s=[round(x, 3) for x in enc["enc_s"]],
                    dec_s=[round(x, 4) for x in dec], **q)
        cells.append(cell)
        print(f"  [prores] {prof:9} {cell['mbps']:8.1f} Mbps "
              f"Y {cell['psnr_y']:6.2f} dB SSIM {cell['ssim_y']:.5f} | "
              f"enc {cell['enc_ms']:7.1f} ms/f dec {cell['dec_ms']:6.1f} ms/f "
              f"profile={enc['probe'].get('profile')}", flush=True)

    # —— DNxHR 参照（10-bit 主行 + 8-bit 附加行） ——
    if not fam["dnxhr"] and not fam["dnxhr_supp"]:
        print("  [dnxhr ] 跳过：该家族无 10-bit DNxHR 对应（44412 无 12-bit）",
              flush=True)
    for key, pf, bd in fam["dnxhr"] + fam["dnxhr_supp"]:
        if not want_ref("dnxhr", key):
            continue
        product = BENCH / f"dnxhr_{key}_{RES_NAME_CUR[0]}.mov"
        if product.exists():
            product.unlink()
        frames = build_av_frames(raw, w, h, n, pf, bd, fam["bd"])
        enc = ref_encode_cell("dnxhd", DNX_OPT[key], w, h, pf, frames,
                              args.runs, threads, product)
        del frames
        dec = ref_decode_cell(enc["product"], args.runs, threads)
        mbps = Path(enc["product"]).stat().st_size * 8 / (frames_total / FPS) / 1e6
        q = quality_metrics(codec, enc["product"], "dnxhr", src_planes,
                            w, h, measure)
        cell = dict(res=res, family=fam_name, codec="dnxhr", variant=key,
                    bit_depth=bd, supplementary=bd < fam["bd"],
                    mbps=round(mbps, 2),
                    bpp=round(mbps * 1e6 / (25 * w * h), 3),
                    enc_ms=round(enc["enc_median"] / n * 1000, 2),
                    enc_fps=round(n / enc["enc_median"], 1),
                    dec_ms=round(median(dec) * 1000, 2),
                    dec_fps=round(1 / median(dec), 1),
                    probe=enc["probe"], profile_opt=enc["profile_opt"],
                    enc_s=[round(x, 3) for x in enc["enc_s"]],
                    dec_s=[round(x, 4) for x in dec], **q)
        tag = "dnxhr8" if bd < 10 else "dnxhr "
        print(f"  [{tag}] {key:9} {cell['mbps']:8.1f} Mbps "
              f"Y {cell['psnr_y']:6.2f} dB SSIM {cell['ssim_y']:.5f} | "
              f"enc {cell['enc_ms']:7.1f} ms/f dec {cell['dec_ms']:6.1f} ms/f "
              f"bd{bd} profile={enc['probe'].get('profile')}", flush=True)
        cells.append(cell)

    # —— 分析：RD 插值 Δ（只对同位深主行）+ 配对速度比 ——
    def line_of(kind):
        return sorted([c for c in cells if c["codec"] == kind and
                       not c["supplementary"] and c["bit_depth"] == fam["bd"]],
                      key=lambda c: c["mbps"])

    deltas = []
    for t in [c for c in cells if c["codec"] == "topos"]:
        row = dict(tier=t["variant"], mbps=t["mbps"], psnr_y=t["psnr_y"])
        for kind in ("prores", "dnxhr"):
            ip = interp_line(line_of(kind), t["mbps"])
            row[f"{kind}_interp_db"] = None if ip is None else round(ip, 3)
            row[f"delta_vs_{kind}_db"] = (None if ip is None
                                          else round(t["psnr_y"] - ip, 3))
        deltas.append(row)
    speed_pairs = []

    def by(k, v):
        c = next((x for x in cells if x["codec"] == k and
                  x["variant"] == v and not x["supplementary"]), None)
        if c is None:  # 附加行回退（如 44412 的深度受限 XQ）
            c = next((x for x in cells if x["codec"] == k and
                      x["variant"] == v), None)
        return c

    for t_id, pr_id, dx_id in fam["pairs"]:
        t, pr = by("topos", t_id), by("prores", pr_id)
        dx = by("dnxhr", dx_id) if dx_id else None
        if not t or not pr:
            continue
        speed_pairs.append(dict(
            tier=t_id,
            enc_vs_prores=round(t["enc_ms"] / pr["enc_ms"], 3),
            dec_vs_prores=round(t["dec_ms"] / pr["dec_ms"], 3),
            enc_vs_dnxhr=(None if not dx else
                          round(t["enc_ms"] / dx["enc_ms"], 3)),
            dec_vs_dnxhr=(None if not dx else
                          round(t["dec_ms"] / dx["dec_ms"], 3))))
    return dict(res=res, family=fam_name, frames_total=frames_total,
                measure=measure, cells=cells, rd_deltas=deltas,
                speed_pairs=speed_pairs)


def write_md(evidence: Path, groups):
    lines = ["# Topos × ProRes × DNxHR 对照组（自动生成）", ""]
    for g in groups:
        lines.append(f"## {g['res'].upper()} / {g['family']}"
                     f"（{g['frames_total']} 帧 @25fps，画质测前 {g['measure']} 帧）")
        lines.append("")
        main = [c for c in g["cells"] if not c["supplementary"]]
        supp = [c for c in g["cells"] if c["supplementary"]]
        lines.append("| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y |"
                     " enc ms/f | dec ms/f |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for c in sorted(main, key=lambda c: c["mbps"]):
            lines.append(
                f"| {c['codec']} | {c['variant']} | {c['bit_depth']} |"
                f" {c['mbps']:.1f} | {c['psnr_y']:.2f} | {c['ssim_y']:.5f} |"
                f" {c['enc_ms']:.1f} | {c['dec_ms']:.1f} |")
        if supp:
            lines.append("")
            lines.append("附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：")
            lines.append("")
            lines.append("| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y |"
                         " enc ms/f | dec ms/f |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for c in sorted(supp, key=lambda c: c["mbps"]):
                lines.append(
                    f"| {c['codec']} | {c['variant']} | {c['bit_depth']} |"
                    f" {c['mbps']:.1f} | {c['psnr_y']:.2f} |"
                    f" {c['ssim_y']:.5f} | {c['enc_ms']:.1f} |"
                    f" {c['dec_ms']:.1f} |")
        lines.append("")
        if g["rd_deltas"]:
            lines.append("同码率插值 Δ（topos − 参照，dB；仅同位深主行）：")
            for d in g["rd_deltas"]:
                dp = d["delta_vs_prores_db"]
                dd = d["delta_vs_dnxhr_db"]
                lines.append(
                    f"- {d['tier']}: vs ProRes "
                    f"{('n/a' if dp is None else f'{dp:+.2f}')} dB，"
                    f"vs DNxHR "
                    f"{('n/a' if dd is None else f'{dd:+.2f}')} dB"
                    f"（{d['mbps']:.0f} Mbps）")
            lines.append("")
        if g["speed_pairs"]:
            lines.append("配对速度比（耗时比，>1 = topos 更慢）：")
            for s in g["speed_pairs"]:
                ed = s["enc_vs_dnxhr"]
                dd = s["dec_vs_dnxhr"]
                lines.append(
                    f"- {s['tier']}: enc {s['enc_vs_prores']}×ProRes / "
                    f"{('n/a' if ed is None else f'{ed}')}×DNxHR，"
                    f"dec {s['dec_vs_prores']}×ProRes / "
                    f"{('n/a' if dd is None else f'{dd}')}×DNxHR")
            lines.append("")
    (evidence / "codecs_matrix.md").write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--res", choices=["2k", "4k", "both"], default="both")
    ap.add_argument("--families", default="422,44410,44412",
                    help="逗号分隔：422,44410,44412")
    ap.add_argument("--tiers", default="",
                    help="逗号分隔的 topos 档位过滤（空=全部）")
    ap.add_argument("--refs", default="",
                    help="逗号分隔 prores:lt / dnxhr:hqx 过滤（空=全部）")
    ap.add_argument("--frames", type=int, default=96)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--entropy", choices=["v2", "v1"], default="v2",
                    help="topos 熵模式（默认 v2，产品编码器同口径）")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()
    args.families = [x.strip() for x in args.families.split(",") if x.strip()]
    args.tiers = [x.strip() for x in args.tiers.split(",") if x.strip()]
    args.refs = [x.strip() for x in args.refs.split(",") if x.strip()]

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    import src.shared.codec.topos_binding as topos_binding
    from src.shared.codec.topos_binding import ToposCodec
    codec = ToposCodec()
    codec.set_slice_threads(args.threads)

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
    print(f"[pyav] {av.__version__} / "
          f"{getattr(av, 'ffmpeg_version_info', 'ffmpeg?')[:48]}")
    print(f"[scope] threads={args.threads}, frames={args.frames}, "
          f"runs={args.runs}, res={args.res}, families={args.families}")

    result = {"manifest": manifest,
              "meta": dict(threads=args.threads, frames=args.frames,
                           runs=args.runs, fps=FPS, pyav=av.__version__,
                           pyav_ffmpeg=getattr(av, "ffmpeg_version_info", ""),
                           entropy=args.entropy),
              "groups": []}
    res_list = ["2k", "4k"] if args.res == "both" else [args.res]
    for res in res_list:
        for fam in args.families:
            print(f"\n===== {RES_NAME[res]} / {fam} =====", flush=True)
            RES_NAME_CUR[0] = RES_NAME[res]
            g = run_family(res, fam, codec, topos_binding, args, args.threads)
            if g:
                result["groups"].append(g)

    (out_dir / "codecs_matrix.json").write_text(
        json.dumps(result, indent=1, ensure_ascii=False))
    write_md(out_dir, result["groups"])
    print(f"\n[result] {out_dir / 'codecs_matrix.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
