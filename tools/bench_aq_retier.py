"""V2.x AQ 增益钉扎：V2+AQ 同画质（逐帧 Y-PSNR 匹配）下的 bpp 重标空间测量。

用途：判断 AQ（色度专属逐带偏移，reserved[1]=1）是否值得触发第二轮
P4 档位重定标。决策口径：安全比例需显著低于 1（建议 ≤0.93，qp 粒度
~10%/档）才值得调档；结果与判据一并写入 native/topos_codec/docs 计划表销账。

方法（每 tier × 分辨率，逐帧钉扎）：
1. 锚点 = 现行生产口径：V2 熵 + AQ 关 + P4 target（ANCHOR_BPP）sized
   搜索 → qp_i（确定性唯一）；该 qp 固定编码 B_base 并解码 → Y-PSNR_base
   （质量锚——与今日交付逐字节一致）。
2. 候选 = V2+AQ 同帧 qp_i+2 / qp_i+1 / qp_i（升 qp 省字节，字节最少优先），
   取首个 bytes < B_base 且 Y-PSNR ≥ PSNR_base − TOL 的候选。
   qp_i==63（qp 地板）：AQ 只会更胖（+偏移被钳 63、−偏移细化）——按生产
   qp63 守卫直接取锚点：该帧字节被地板+守卫钳住，不随 bpp 变化。
3. cell ratio = ΣB_aq / ΣB_base；tier 安全比例 = **非地板** cell 的最大值
   （地板 cell 行为与 bpp 无关，不构成约束；max 方向 = 其余 cell 的
   sized 搜索落点 qp ≤ 匹配 qp ⇒ 画质不低于锚）。

用法：.venv/bin/python tools/bench_aq_retier.py [--frames 48] [--tol 0.05]
      [--tiers proxy,lt,...] [--res 2k,4k] [--allow-dirty]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402
from bench_rd_baseline import psnr  # noqa: E402

BENCH = PROJECT_ROOT / "bench_out" / "codec_bench"
RES = {"2k": dict(w=1920, h=1080), "4k": dict(w=3840, h=2160)}
RES_NAME = {"2k": "2K", "4k": "4K"}

TIERS = [
    ("proxy", 3, 0, 10, "422"), ("lt", 3, 0, 10, "422"),
    ("standard", 3, 0, 10, "422"), ("hq", 3, 0, 10, "422"),
    ("pro444", 5, 1, 10, "444"), ("extreme", 6, 1, 12, "444"),
]
RAW = {
    ("422", 10): {"2K": "raw_2K.yuv", "4K": "raw_4K.yuv"},
    ("444", 10): {"2K": "raw_2K444p10.yuv", "4K": "raw_4K444p10.yuv"},
    ("444", 12): {"2K": "raw_2K444p12.yuv", "4K": "raw_4K444p12.yuv"},
}

# AQ 轮锚点 = P4 落地值（2026-09-03，V2 转正口径）。硬编码防自引用：
# 本工具结果落回 topos_profiles 后不得漂移锚点。
ANCHOR_BPP = {"proxy": 0.60509, "lt": 1.23239, "standard": 1.87491,
              "hq": 2.75617, "pro444": 4.11063, "extreme": 6.76296}


def frame_config(binding, w, h, profile, pf, bd, chroma_off, aq, rdo=0):
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
    fc.reserved[0] = 1              # V2（生产默认）
    fc.reserved[1] = 1 if aq else 0
    fc.reserved[2] = 1 if rdo else 0
    fc.color_range = fc.color_primaries = fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num = fc.sar_den = 1
    return fc


def load_frames(raw, w, h, n, domain):
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--tol", type=float, default=0.05,
                    help="逐帧 Y-PSNR 容差 dB（默认 0.05 = 验收门槛）")
    ap.add_argument("--tiers", type=str, default="")
    ap.add_argument("--res", type=str, default="")
    ap.add_argument("--rdo", action="store_true",
                    help="候选编码启用逐系数 level RDO（reserved[2]=1）")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()

    want_tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    want_res = {r.strip() for r in args.res.split(",") if r.strip()}

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    import src.shared.codec.topos_binding as tb
    from src.shared.codec.topos_binding import ToposCodec, ToposDecoder
    from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS
    codec = ToposCodec()
    codec.set_slice_threads(16)

    import hashlib
    m8 = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:8]
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (PROJECT_ROOT / "bench_out" / manifest["git_head"]
               / manifest["dylib_sha256"][:8] / m8 / utc)
    out_dir.mkdir(parents=True, exist_ok=False)

    print("[manifest]", manifest["git_head"], "dirty" if manifest["git_dirty"]
          else "clean", f"frames={args.frames} tol={args.tol}dB")
    rows = []
    print(f"{'tier':9} {'res':3} {'qp[min/med/max]':>15} {'floor%':>6} "
          f"{'match%':>6} {'dqp 分布':>16} {'ratio':>7} {'new MB/s':>9}")
    for tier_id, profile, pf, bd, domain in TIERS:
        if want_tiers and tier_id not in want_tiers:
            continue
        tier_def = TOPOS_PROFILE_TIERS[tier_id]
        chroma_off = int(getattr(tier_def, "chroma_qp_offset", 0) or 0)
        anchor_bpp = ANCHOR_BPP[tier_id]
        peak = float((1 << bd) - 1)
        ratios, cells = [], []
        for res, rc in RES.items():
            if want_res and res not in want_res:
                continue
            w, h = rc["w"], rc["h"]
            raw = BENCH / RAW[(domain, bd)][RES_NAME[res]]
            frames = load_frames(raw, w, h, args.frames, domain)
            target = max(1, int(round(anchor_bpp * w * h / 8.0)))

            fc_base = frame_config(tb, w, h, profile, pf, bd, chroma_off, 0)
            fc_aq = frame_config(tb, w, h, profile, pf, bd, chroma_off,
                                 0 if args.rdo else 1,
                                 1 if args.rdo else 0)

            # 解码器按 cell 建一次（几何固定；prepare 吃首个包）
            dec = ToposDecoder(codec)
            state = {}

            def decode_y(pkt):
                if "planes" not in state:
                    info = dec.prepare(pkt, len(pkt))
                    pc = int(info.plane_count)
                    planes = []
                    for p in range(pc):
                        pw, ph = codec.plane_geometry(info, p)
                        planes.append(np.empty((ph, pw), dtype="<u2"))
                    state["views"] = dec.make_views(planes)
                    state["pc"] = pc
                    state["planes"] = planes
                dec.decode_views(pkt, state["views"], state["pc"], len(pkt))
                return state["planes"][0]

            qps, dqp_hist = [], {2: 0, 1: 0, 0: 0, -1: 0}
            b_base_sum, b_aq_sum = 0, 0
            floor_n = 0
            for planes_in in frames:
                _pkt, _st, qp_i = codec.encode_sized(fc_base, planes_in, target)
                qp_i = int(qp_i)
                qps.append(qp_i)
                # 锚点：qp_i 固定编码（= 今日生产字节）+ 解码质量锚
                fc_base.qp_base = qp_i
                pkt_b, _ = codec.encode_frame(fc_base, planes_in)
                psnr_b = psnr(planes_in[0], decode_y(pkt_b), peak)
                b_base_sum += len(pkt_b)

                if qp_i >= 63:
                    # qp 地板：AQ 只增字节——生产守卫关 AQ，锚点即结果
                    floor_n += 1
                    b_aq_sum += len(pkt_b)
                    dqp_hist[-1] += 1
                    continue
                # 候选：升 qp 省字节优先（dqp=2 → 1 → 0），同画质门槛
                chosen = None
                for dqp in (2, 1, 0):
                    qp_c = qp_i + dqp
                    if qp_c > 63:
                        continue
                    fc_aq.qp_base = qp_c
                    pkt_c, _ = codec.encode_frame(fc_aq, planes_in)
                    if len(pkt_c) >= len(pkt_b):
                        continue
                    if psnr(planes_in[0], decode_y(pkt_c), peak) \
                            >= psnr_b - args.tol:
                        chosen = (dqp, len(pkt_c))
                        break
                if chosen is None:
                    b_aq_sum += len(pkt_b)
                    dqp_hist[-1] += 1
                else:
                    dqp, nb = chosen
                    b_aq_sum += nb
                    dqp_hist[dqp] += 1

            dec.close()
            del frames

            ratio = b_aq_sum / max(1, b_base_sum)
            ratios.append(ratio)
            qp_mn, qp_md, qp_mx = min(qps), int(np.median(qps)), max(qps)
            floor_cell = (qp_mn >= 63)
            cells.append(floor_cell)
            match_pct = 100.0 * (dqp_hist[2] + dqp_hist[1] + dqp_hist[0]) \
                / max(1, len(qps))
            mbps_new = anchor_bpp * ratio * 1920 * 1080 * 25 / 1e6
            rows.append(dict(
                tier=tier_id, res=res, frames=len(qps),
                qp_min=qp_mn, qp_median=qp_md, qp_max=qp_mx,
                floor_frames=floor_n, floor_cell=floor_cell,
                dqp_hist=dqp_hist, match_pct=round(match_pct, 1),
                b_base=b_base_sum, b_aq=b_aq_sum, ratio=round(ratio, 4),
                anchor_target_bpp=anchor_bpp,
                new_target_bpp=round(anchor_bpp * ratio, 5),
                mbps_1080p25_new=round(mbps_new, 1)))
            r = rows[-1]
            hist = "/".join(f"{k}:{v}" for k, v in
                            sorted(dqp_hist.items(), reverse=True))
            print(f"{tier_id:9} {res:3} {qp_mn:4d}/{qp_md:4d}/{qp_mx:4d} "
                  f"{100.0 * floor_n / max(1, len(qps)):6.1f} "
                  f"{match_pct:6.1f} {hist:>16} {ratio:7.4f} "
                  f"{mbps_new:9.1f}", flush=True)

        if not ratios:
            continue
        # 安全比例：地板 cell（全部帧 qp63）被 qp 上限+生产守卫钳住，
        # 字节不随 bpp 变化 → 不构成约束；其余 cell 取 max（cut 最浅侧）
        pair = list(zip(ratios, cells))
        binding = [r for r, fl in pair if not fl]
        safe = max(binding) if binding else 1.0
        print(f"  → {tier_id}: cell 比率 {['%.4f%s' % (r, 'F' if fl else '')
              for r, fl in pair]}，安全比例（非地板 max）{safe:.4f}，"
              f"建议 bpp {anchor_bpp:.5f} → {anchor_bpp * safe:.5f}\n",
              flush=True)
        rows.append(dict(tier=tier_id, res="SAFE", ratios=ratios,
                         floor_cells=cells, safe_ratio=round(safe, 4),
                         anchor_target_bpp=anchor_bpp,
                         new_target_bpp=round(anchor_bpp * safe, 5)))

    (out_dir / "aq_retier.json").write_text(
        json.dumps(dict(manifest=manifest, args=vars(args), rows=rows),
                   indent=1, ensure_ascii=False))
    print(f"[result] {out_dir / 'aq_retier.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
