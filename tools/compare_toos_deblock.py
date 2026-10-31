#!/usr/bin/env python3
"""图片链路（.toos）去块滤波前后对比：同一码流、只换解码库。

去块是**纯解码端**输出滤波（位流 ``flags`` bit2），故"前后"只换 dylib 不换
码流。本工具把视频链路的 A/B 口径（``tools/compare_topos_deblock.py``）搬到
**图片链路**，回答三件事：

1. App 写的 ``.toos``（4:2:2 10-bit，导出器设 ``reserved[5]`` bit0 → flags
   bit2=开）是否随视频链路一起改善；
2. ``toos encode`` CLI 写的 ``.toos``（CLI 从不设 ``reserved[5]`` → bit2=关）
   是否也在内；
3. RAW（pf=3 / 12-bit / profile 7）是否受影响——域外，应为**字节相同**；
   另附"开位被域校验拒绝"的取证（pf=3 时 ``reserved[5]`` 非法）。

另附"开关零代价"取证：同一帧 features=0/1 两次编码的包**长度相同**，且差异
只落在帧头若干字节内（coded payload 逐字节相同）——图片体积零影响。

用法::

    .venv/bin/python tools/compare_toos_deblock.py --size 2K --frame 240 \
      --old-lib /tmp/topos_prefix_wt/native/topos_codec/build/release/topos_codec.dylib \
      --new-lib native/topos_codec/build/release/topos_codec.dylib
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from bench_topos_qgain import iter_master  # noqa: E402
from compare_topos_deblock import SIZES, metrics  # noqa: E402
from src.shared.codec.topos_binding import (  # noqa: E402
    TOPOS_CODEC_ABI_VERSION, ToposCodec, _CFrameConfig,
)
from src.shared.codec.topos_image_binding import (  # noqa: E402
    TC_IMG_PROFILE_RAW, ToposImageCodec,
)

# 图片档位（native/topos_codec/src/cli/image_cli.c kTierPresets 同名同参）
STILL_TIERS = {
    "422-ultra": dict(pf=0, bd=10, qm=0, qp=58, image_profile=0),
    "422-high": dict(pf=0, bd=10, qm=1, qp=58, image_profile=0),
}
# RAW 12-bit 档位（pf=3 / profile 7 / qm0 冻结 / transfer LOG0）
RAW_TIERS = {
    "raw12-2": 45, "raw12-4": 59, "raw12-8": 66,
}


def _fc(w: int, h: int, *, pf: int, bd: int, qm: int, qp: int,
        features: int = 0, profile: int = 3) -> _CFrameConfig:
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width = w
    fc.visible_height = h
    fc.profile = profile
    fc.pixel_format = pf
    fc.bit_depth = bd
    fc.qmatrix_id = qm
    fc.qp_base = qp
    fc.color_transfer = 20 if (pf == 3 and bd == 12) else 1
    fc.reserved[0] = 8  # V7-R2 rans2：图片链路现行默认（ADR-C038）
    fc.reserved[1] = 1
    fc.reserved[5] = features
    return fc


def encode(codec: ToposCodec, w: int, h: int, planes: list[np.ndarray],
           image_profile: int = 0, **kw) -> bytes:
    """编一帧；``image_profile`` 属 .toos 信封参数，编帧时忽略。"""
    data = [np.ascontiguousarray(p, dtype="<u2").tobytes() for p in planes]
    pkt, _stats = codec.encode_frame(_fc(w, h, **kw), data)
    return bytes(pkt)


def decode(lib: str, path: Path, target: tuple | None = None):
    ic = ToposImageCodec(lib)
    arrays, info, meta = ic.decode(str(path), target_size=target)
    return [np.asarray(a, dtype=np.uint16) for a in arrays], info, meta


def _header_diff(a: bytes, b: bytes) -> dict:
    """两包差异落位：长度、首个差异偏移、差异是否全在帧头区（前 128B）。"""
    n = min(len(a), len(b))
    d = [i for i in range(n) if a[i] != b[i]]
    return {
        "len_a": len(a), "len_b": len(b),
        "first_diff": d[0] if d else None,
        "diff_bytes": len(d),
        "diff_in_header": bool(d) and d[-1] < 128,
        "tail_sha_same": hashlib.sha256(a[128:]).hexdigest()
        == hashlib.sha256(b[128:]).hexdigest(),
    }


def cfa_from_master(ry: np.ndarray, ru: np.ndarray, rv: np.ndarray) -> list[np.ndarray]:
    """母版 10-bit YUV（BT.709 limited）→ 12-bit RGB → RGGB 四相位下采样。

    合成 CFA（非真传感器 DNG）：只用于验证"去块域外 + 档位画质口径"，
    绝对比率/PSNR 不代表真实 RAW 素材（真 DNG 标定见
    tools/traw_batch4_2026-09-13.json）。
    """
    y = (ry.astype(np.float64) - 64.0) / 876.0
    u = np.repeat((ru.astype(np.float64) - 512.0) / 896.0, 2, axis=1)[:, :ry.shape[1]]
    v = np.repeat((rv.astype(np.float64) - 512.0) / 896.0, 2, axis=1)[:, :ry.shape[1]]
    r = y + 1.5748 * v
    g = y - 0.1873 * u - 0.4681 * v
    b = y + 1.8556 * u
    rgb = np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0) * 4095.0
    return [np.rint(rgb[0::2, 0::2, 0]).astype(np.uint16),   # R
            np.rint(rgb[0::2, 1::2, 1]).astype(np.uint16),   # Gr
            np.rint(rgb[1::2, 0::2, 1]).astype(np.uint16),   # Gb
            np.rint(rgb[1::2, 1::2, 2]).astype(np.uint16)]   # B


def _psnr_plane(a: np.ndarray, b: np.ndarray, peak: float) -> float:
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(d * d))
    return float("nan") if mse <= 0 else 10.0 * np.log10(peak * peak / mse)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="2K", choices=tuple(SIZES))
    ap.add_argument("--frame", type=int, default=240)
    ap.add_argument("--old-lib", required=True, help="改动前库（v2.0 门控）")
    ap.add_argument("--new-lib", required=True, help="改动后库（v2.1）")
    ap.add_argument("--raw-tiers", default="raw12-2,raw12-4,raw12-8")
    ap.add_argument("--knobs", default="",
                    help="给 TC_DB_TUNE 构建的滤波旋钮（如 "
                         "'DB_STEP_NUM=35,DB_STEP_DEN=100000'）——旋钮是进程级"
                         "环境变量，故自动 re-exec 一次；release 库忽略这些值")
    ap.add_argument("--out", type=Path, default=REPO / "bench_out" / "qgain")
    args = ap.parse_args()

    if args.knobs and os.environ.get("_TOOS_DB_KNOBS") != args.knobs:
        env = dict(os.environ)
        for kv in args.knobs.split(","):
            k, _, v = kv.partition("=")
            env[k.strip()] = v.strip()
        env["_TOOS_DB_KNOBS"] = args.knobs
        print(f"[knobs] re-exec with {args.knobs}", flush=True)
        os.execve(sys.executable,
                  [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                  env)

    w, h = SIZES[args.size]
    codec = ToposCodec()  # 编码端与滤波无关：全程用默认（HEAD）库编码
    tmp = Path(tempfile.mkdtemp(prefix="toos_deblock_"))
    ref = iter_master("yuv422p10le", w, h, {args.frame})
    ry, ru, rv = (np.ascontiguousarray(p) for p in ref[args.frame])
    report: dict = {"size": args.size, "frame": args.frame,
                    "master": f"tests/test_videos/4K_prores4444.mov → {w}x{h}",
                    "old_lib": args.old_lib, "new_lib": args.new_lib,
                    "still": {}, "still_preview": {}, "raw": {}}

    # —— 1) 4:2:2 图片：features=0（CLI 等价）vs features=1（App 路径）——
    for tier, spec in STILL_TIERS.items():
        row: dict = {}
        pkts = {}
        for feat in (0, 1):
            pkt = encode(codec, w, h, [ry, ru, rv], features=feat, **spec)
            pkts[feat] = pkt
            toos = tmp / f"{tier}_f{feat}.toos"
            ToposImageCodec().write_packet(str(toos), pkt, spec["image_profile"],
                                           durable=False)
            row[f"toos_bytes_f{feat}"] = toos.stat().st_size
            for tag, lib in (("old", args.old_lib), ("new", args.new_lib)):
                planes, _info, meta = decode(lib, toos)
                if feat == 1 or tag == "old":
                    # f0 两库同哈希（无滤波），取一次即可；f1 分前后两列
                    row[f"{tag}_f{feat}"] = {
                        **metrics(ref, {args.frame: planes[0]}),
                        "decode_mode": meta["decode_mode"]}
                row[f"{tag}_f{feat}_y_sha"] = hashlib.sha256(
                    planes[0].tobytes()).hexdigest()[:16]
        row["packet_diff"] = _header_diff(pkts[0], pkts[1])
        # CLI 等价档（features=0）在前后库上应完全相同
        row["f0_old_eq_new"] = row["old_f0_y_sha"] == row["new_f0_y_sha"]
        # App 档（features=1）在前后库上应不同 —— 即"图片链路确实吃到修复"
        row["f1_old_eq_new"] = row["old_f1_y_sha"] == row["new_f1_y_sha"]
        if row["old_f1_y_sha"] != row["new_f1_y_sha"]:
            po, _i, _m = decode(args.old_lib, tmp / f"{tier}_f1.toos")
            pn, _i, _m = decode(args.new_lib, tmp / f"{tier}_f1.toos")
            row["old_vs_new_pixels_differ"] = int(np.count_nonzero(po[0] != pn[0]))
        report["still"][tier] = row

    # —— 1b) 预览/缩略图（缩放交付，unit=4 目标网格滤波）：母版半尺寸参照 ——
    tw, th = w // 2, h // 2
    ref_half = iter_master("yuv422p10le", tw, th, {args.frame})
    for tier in STILL_TIERS:
        toos = tmp / f"{tier}_f1.toos"
        row = {}
        for tag, lib in (("old", args.old_lib), ("new", args.new_lib)):
            planes, _info, meta = decode(lib, toos, target=(tw, th))
            row[tag] = {**metrics(ref_half, {args.frame: planes[0]}),
                        "decode_mode": meta["decode_mode"],
                        "y_sha": hashlib.sha256(planes[0].tobytes()).hexdigest()[:16]}
        report["still_preview"][tier] = row

    # —— 2) RAW：域外，前后库应逐字节相同 ——
    cfa = cfa_from_master(ry, ru, rv)
    pw = (w + 1) // 2
    ph = (h + 1) // 2
    for tier, qp in ((t, RAW_TIERS[t]) for t in args.raw_tiers.split(",")):
        pkt = encode(codec, w, h, cfa, pf=3, bd=12, qm=0, qp=qp, profile=0)
        toos = tmp / f"{tier}.toos"
        ToposImageCodec().write_packet(str(toos), pkt, TC_IMG_PROFILE_RAW,
                                       durable=False)
        outs = {}
        for tag, lib in (("old", args.old_lib), ("new", args.new_lib)):
            planes, _info, _meta = decode(lib, toos)
            outs[tag] = planes
        same = all(out_old.shape == out_new.shape
                   and np.array_equal(out_old, out_new)
                   for out_old, out_new in zip(outs["old"], outs["new"]))
        psnr = [_psnr_plane(cfa[i], outs["new"][i], 4095.0) for i in range(4)]
        report["raw"][tier] = {
            "qp": qp, "toos_bytes": toos.stat().st_size,
            "raw_bytes": int(w * h * 2),
            "ratio": round(w * h * 2 / toos.stat().st_size, 2),
            "planes": len(outs["new"]),
            "plane_shape": list(outs["new"][0].shape),
            "old_eq_new_bytes": same,
            "cfa_psnr_db_mean": round(float(np.mean(psnr)), 2),
            "cfa_psnr_db": [round(x, 2) for x in psnr],
        }
    # pf=3 开输出特性位：域校验必须直接拒绝
    try:
        encode(codec, w, h, cfa, pf=3, bd=12, qm=0, qp=59, profile=0, features=1)
        report["raw"]["features_on_rejected"] = False
    except Exception as exc:  # noqa: BLE001 —— 取证：消息即证据
        report["raw"]["features_on_rejected"] = True
        report["raw"]["features_on_error"] = str(exc)[:200]

    # —— 打印 ——
    for tier, row in report["still"].items():
        print(f"\n=== {args.size} 图片档 {tier}（同一 coded payload，只换解码库/标志位）===")
        print(f"{'指标':<16}{'无滤波(f0)':>13}{'v2.0(f1)':>12}{'v2.1(f1)':>12}"
              f"{'v2.1-前':>11}{'v2.1-无滤':>11}")
        keys = ("block_h", "block_v", "y_psnr", "y_psnr_edge", "sharp", "grid",
                "stair")
        for k in keys:
            f0 = row["old_f0"].get(k)
            a = row["old_f1"].get(k)
            b = row["new_f1"].get(k)
            print(f"{k:<16}{f0:>13.4f}{a:>12.4f}{b:>12.4f}{b - a:>+11.4f}"
                  f"{b - f0:>+11.4f}")
        print(f"包长 f0/f1 = {row['packet_diff']['len_a']}/{row['packet_diff']['len_b']}"
              f"  差异字节 {row['packet_diff']['diff_bytes']}"
              f"（首差 {row['packet_diff']['first_diff']}，全在帧头 "
              f"{row['packet_diff']['diff_in_header']}，尾段同 SHA "
              f"{row['packet_diff']['tail_sha_same']}）")
        print(f"f0 前后库同哈希 {row['f0_old_eq_new']}；"
              f"f1 前后库同哈希 {row['f1_old_eq_new']}；"
              f"f1 差异像素 {row.get('old_vs_new_pixels_differ', 0)}")

    print(f"\n=== {args.size} 图片预览（target {tw}x{th}，缩放交付目标网格滤波）===")
    for tier, row in report["still_preview"].items():
        print(f"{tier:<12}模式 {row['new']['decode_mode']:<12}"
              f"block_h {row['old']['block_h']:.4f} → {row['new']['block_h']:.4f}"
              f"  block_v {row['old']['block_v']:.4f} → {row['new']['block_v']:.4f}"
              f"  y_psnr {row['old']['y_psnr']:.2f} → {row['new']['y_psnr']:.2f}"
              f"  库间同哈希 {row['old']['y_sha'] == row['new']['y_sha']}")

    print(f"\n=== {args.size} RAW（pf=3 / 12-bit / profile 7）===")
    for tier, row in report["raw"].items():
        if not isinstance(row, dict):
            continue
        print(f"{tier:<10} qp{row['qp']:<4} {row['toos_bytes'] / 1e6:6.2f} MB "
              f"({row['ratio']:5.2f}:1)  CFA PSNR {row['cfa_psnr_db_mean']:5.2f} dB  "
              f"前后库字节相同 {row['old_eq_new_bytes']}")
    print(f"pf=3 开 DEBLOCK 位被拒 {report['raw'].get('features_on_rejected')}："
          f"{report['raw'].get('features_on_error', '')}")

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"toos_deblock_cmp_{args.size}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"\nreport → {out}\n临时文件 {tmp}（可删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
