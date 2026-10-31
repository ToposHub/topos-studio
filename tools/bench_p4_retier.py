"""P4 档位重定标测量：质量钉住的 target_bpp 重标比例（V2 转正后）。

方法（每 tier × 分辨率）：
1. V1 口径 sized 编码（现行 target）→ 逐帧 qp_used（确定性搜索的选择）；
2. 同帧同 qp 固定编码两次：V1（校验 B1≈target，搜索成功的锚点）与
   V2（B2 = 同画质下的新字节预算）；
3. ratio = ΣB2 / ΣB1；建议新 target_bpp = 旧 × ratio（tier 内取均值）。

转正后 tier 的 V2 sized 搜索以新 target 落回同 qp ⇒ 同画质、按比例缩体积。
proxy@4K 若 V1 已顶格 qp63，B2/63 即其新地板（超支收敛但不可低于地板）。

用法：.venv/bin/python tools/bench_p4_retier.py [--frames 12] [--allow-dirty]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from bench_manifest import ManifestMismatch, build_manifest  # noqa: E402

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


def frame_config(binding, w, h, profile, pf, bd, chroma_off, entropy_v2):
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
    fc.reserved[0] = 1 if entropy_v2 else 0
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
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()

    try:
        manifest = build_manifest(allow_dirty=args.allow_dirty)
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        return 2

    import src.shared.codec.topos_binding as tb
    from src.shared.codec.topos_binding import ToposCodec
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
          else "clean")
    # P4 前的 V1 档位工作点（重定标锚点——本工具不得读取当前
    # target_bpp，否则在 P4 落地后自引用）
    OLD_BPP = {"proxy": 0.89000, "lt": 1.77999, "standard": 2.66999,
               "hq": 3.88361, "pro444": 5.98724, "extreme": 9.14268}
    rows = []
    print(f"{'tier':9} {'res':3} {'target KB/f':>11} {'qp(med)':>8} "
          f"{'B1/T':>6} {'B2/B1':>7} {'new MB/s':>9}")
    for tier_id, profile, pf, bd, domain in TIERS:
        tier_def = TOPOS_PROFILE_TIERS[tier_id]
        chroma_off = int(getattr(tier_def, "chroma_qp_offset", 0) or 0)
        old_bpp = OLD_BPP[tier_id]
        ratios = []
        for res, rc in RES.items():
            w, h = rc["w"], rc["h"]
            raw = BENCH / RAW[(domain, bd)][RES_NAME[res]]
            frames = load_frames(raw, w, h, args.frames, domain)
            target = max(1, int(round(old_bpp * w * h / 8.0)))

            # 逐帧钉扎：V1 sized（旧目标）选 qp_i → 该帧在 qp_i 下
            # 固定编码 V1（锚点）/V2（预算）——精确口径，无中位数近似
            fc1 = frame_config(tb, w, h, profile, pf, bd, chroma_off, False)
            qps, b1, b2, b_target = [], 0, 0, 0
            for planes in frames:
                _pkt, _st, qp = codec.encode_sized(fc1, planes, target)
                qp = int(qp)
                qps.append(qp)
                b_target += target
                fcv1 = frame_config(tb, w, h, profile, pf, bd, chroma_off,
                                    False)
                fcv1.qp_base = qp
                pkt, _st2 = codec.encode_frame(fcv1, planes)
                b1 += len(pkt)
                fcv2 = frame_config(tb, w, h, profile, pf, bd, chroma_off,
                                    True)
                fcv2.qp_base = qp
                pkt, _st3 = codec.encode_frame(fcv2, planes)
                b2 += len(pkt)
            qp_med = int(statistics.median(qps))
            ratio = b2 / max(1, b1)
            ratios.append(ratio)
            mbps_old = old_bpp * w * h * 25 / 1e6
            rows.append(dict(tier=tier_id, res=res, qp_median=qp_med,
                             qp_all=qps, target_bytes=int(target),
                             b1=int(b1), b2=int(b2), ratio=round(ratio, 4),
                             old_target_bpp=old_bpp,
                             new_target_bpp=round(old_bpp * ratio, 5),
                             mbps_1080p25_new=round(
                                 old_bpp * ratio * 1920 * 1080 * 25 / 1e6, 1)))
            r = rows[-1]
            print(f"{tier_id:9} {res:3} {target/1024:11.1f} {qp_med:8d} "
                  f"{b1/b_target:6.3f} {ratio:7.4f} "
                  f"{r['mbps_1080p25_new']:9.1f}", flush=True)
            del frames
        # 质量安全方向：取各分辨率比例的最大值（任一分辨率落更低的
        # qp 都不会损画质；粗 qp 粒度下均值会把部分帧推高一档）
        safe_ratio = max(ratios)
        print(f"  → {tier_id}: per-res 比率 "
              f"{['%.4f' % r for r in ratios]}，安全比例（max）"
              f"{safe_ratio:.4f}，建议 target_bpp {old_bpp:.5f} → "
              f"{old_bpp * safe_ratio:.5f}\n", flush=True)

    (out_dir / "p4_retier.json").write_text(
        json.dumps(dict(manifest=manifest, rows=rows), indent=1,
                   ensure_ascii=False))
    print(f"[result] {out_dir / 'p4_retier.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
