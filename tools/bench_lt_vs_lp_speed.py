#!/usr/bin/env python3
"""LT vs LP 速度/码率对比（topos_v9_micro_gop_plan §3.10 口径复核）。

口径：
- LT = Topos 422 LT（产品档）：V8 rans2 帧内 + 帧级 sized qp 搜索，
  target_bpp=0.94075（ADR-C032/43 定标，topos_profiles 'lt'）；
- LP = Topos 422 LP（experimental，V9 IP-2）：GOP context 原生链
  （残差伪图 + P≥I 自动回退），固定锚 qp=72（批 6 评审口径，≈150Mbps@4K30 名义）；
- 另测 LT 同 qp（72）固定锚帧内作参照——隔离 sized 搜索开销、给同 qp 对照；
- 内容层窗口与批 0/批 6 相同（tools/lp_spike_2026-09-14.json）；
- 时间只计原生编解码调用（perf_counter 逐帧，双遍取第二遍中位）；
  源解码（PyAV）与 Python 装配不计入；
- 码率 = 实测包字节数均值 × fps（源帧率）。

输出 tools/lt_vs_lp_speed_<date>.json + stdout 表。
"""
from __future__ import annotations

import ctypes
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.shared.codec import topos_binding as tb  # noqa: E402

SRC_4K = REPO / "tests/test_videos/4K_prores422HQ.mov"
SRC_2K = REPO / "tests/test_videos/2K_prores422HQ.mov"
N = 16                     # 每层帧数
PASSES = 2                 # 第一遍预热，第二遍计时
LT_BPP = 0.94075           # topos_profiles 'lt'（ADR-C032/43）
LP_QP = 72                 # 批 6 评审锚（≈150Mbps@4K30 名义）
LP_EMS = (11, 10)          # 载体：11=V7-R3（ADR-C048 产品路径）/ 10=V9 对照
DATE = "2026-09-13"
DATE = "2026-09-13"

LAYERS = [
    ("static_4k", SRC_4K, 425),
    ("handheld_4k", SRC_4K, 100),
    ("broll_4k", SRC_4K, 150),
    ("cut_4k", SRC_4K, 467),
    ("pan_2k", SRC_2K, 226),
    ("texture_2k", SRC_2K, 307),
]


def _planes(frame) -> list[np.ndarray]:
    out = []
    for p in frame.planes:
        arr = np.frombuffer(ctypes.string_at(p.buffer_ptr, p.buffer_size),
                            dtype="<u2").reshape(p.height, p.line_size // 2)
        out.append(np.ascontiguousarray(arr[:frame.height, :p.width]))
    return out


def extract(src: Path, start: int, n: int) -> tuple[list[list[np.ndarray]], float]:
    import av
    cont = av.open(str(src))
    fps = float(cont.streams.video[0].average_rate)
    frames = []
    for i, f in enumerate(cont.decode(video=0)):
        if i >= start:
            frames.append(_planes(f))
            if len(frames) >= n:
                break
    cont.close()
    assert len(frames) == n
    return frames, fps


def base_cfg(w: int, h: int, qp: int, em: int) -> tb._CFrameConfig:
    cfg = tb._CFrameConfig()
    cfg.struct_size = ctypes.sizeof(tb._CFrameConfig)
    cfg.abi_version = tb.TOPOS_CODEC_ABI_VERSION
    cfg.visible_width, cfg.visible_height = w, h
    cfg.profile, cfg.pixel_format, cfg.bit_depth = 3, 0, 10
    cfg.qmatrix_id, cfg.qp_base = 0, qp
    cfg.color_range = cfg.color_primaries = cfg.color_transfer = 1
    cfg.color_matrix = 1
    cfg.reserved[0] = em  # 8=V8 rans2（LT 写端默认）/ 10=V9（LP）
    return cfg


def med_ms(times: list[float]) -> float:
    return statistics.median(times) * 1000.0


def run_layer(codec: tb.ToposCodec, tag: str, src: Path, start: int, lp_em: int = 11) -> dict:
    frames, fps = extract(src, start, N)
    h, w = frames[0][0].shape
    target_bytes = round(LT_BPP * w * h / 8.0)

    out: dict = {"w": w, "h": h, "fps": fps, "n": N, "target_bytes": target_bytes}

    # —— LT：生产 sized（V8 rans2 + bpp 目标 qp 搜索）——
    for _ in range(PASSES):
        enc_t, pkts_lt = [], []
        for f in frames:
            t0 = time.perf_counter()
            pkt, _st, _qp = codec.encode_sized(base_cfg(w, h, LP_QP, 8), f, target_bytes)
            enc_t.append(time.perf_counter() - t0)
            pkts_lt.append(pkt)
    out["lt_sized"] = {
        "encode_ms": med_ms(enc_t), "mean_bytes": statistics.mean(map(len, pkts_lt)),
    }

    # —— LT 参照：同 qp 固定锚帧内（隔离 sized 搜索开销）——
    for _ in range(PASSES):
        enc_t, pkts_ltf = [], []
        for f in frames:
            t0 = time.perf_counter()
            pkt, _st = codec.encode_frame(base_cfg(w, h, LP_QP, 8), f)
            enc_t.append(time.perf_counter() - t0)
            pkts_ltf.append(pkt)
    out["lt_fixed_qp72"] = {
        "encode_ms": med_ms(enc_t), "mean_bytes": statistics.mean(map(len, pkts_ltf)),
    }

    # —— LT 解码（sized 包；持久 decoder context）——
    arrays = [np.zeros(g, np.uint16) for g in ((h, w), (h, w // 2), (h, w // 2))]
    with tb.ToposDecoder(codec) as dec:
        views = dec.make_views(arrays)
        for _ in range(PASSES):
            dec_t = []
            for pkt in pkts_lt:
                t0 = time.perf_counter()
                dec.decode_views(pkt, views, 3)
                dec_t.append(time.perf_counter() - t0)
    out["lt_sized"]["decode_ms"] = med_ms(dec_t)

    # —— LP：帧间微 GOP 原生链（I/P 决策 + 残差 + 回退在原生侧）——
    for _ in range(PASSES):
        enc_t, pkts_lp, types = [], [], []
        with tb.ToposGopContext(codec, base_cfg(w, h, LP_QP, lp_em)) as ctx:
            for f in frames:
                t0 = time.perf_counter()
                pkt, _st = ctx.encode_frame(f)
                enc_t.append(time.perf_counter() - t0)
                pkts_lp.append(pkt)
                types.append(pkt[15])
    out["lp_ip2"] = {
        "encode_ms": med_ms(enc_t), "mean_bytes": statistics.mean(map(len, pkts_lp)),
        "i_frames": types.count(0), "fallbacks_after_first": types[1:].count(0),
    }

    # —— LP 解码（GOP context feed 链，参考推进在原生侧）——
    for _ in range(PASSES):
        dec_t = []
        with tb.ToposGopContext(codec, base_cfg(w, h, LP_QP, lp_em)) as dctx:
            for pkt in pkts_lp:
                t0 = time.perf_counter()
                dctx.feed(pkt, planes=arrays)
                dec_t.append(time.perf_counter() - t0)
    out["lp_ip2"]["decode_ms"] = med_ms(dec_t)

    for k in ("lt_sized", "lt_fixed_qp72", "lp_ip2"):
        d = out[k]
        d["encode_fps"] = 1000.0 / d["encode_ms"]
        if "decode_ms" in d:
            d["decode_fps"] = 1000.0 / d["decode_ms"]
        d["mbps"] = d["mean_bytes"] * 8.0 * fps / 1e6
    return out


def main() -> None:
    codec = tb.ToposCodec()
    results = {}
    for lp_em in LP_EMS:
        for tag, src, start in LAYERS:
            key = f"{tag}" if lp_em == 11 else f"{tag}@v9carrier"
            results[key] = run_layer(codec, tag, src, start, lp_em)
            r = results[key]
            print(f"[{key}] {r['w']}x{r['h']} @{r['fps']:.0f}fps "
                  f"LT sized {r['lt_sized']['mean_bytes']/1e3:.0f}KB "
                  f"({r['lt_sized']['mbps']:.0f}Mbps) enc {r['lt_sized']['encode_fps']:.1f}fps "
                  f"dec {r['lt_sized']['decode_fps']:.1f}fps | "
                  f"LP(em={lp_em}) {r['lp_ip2']['mean_bytes']/1e3:.0f}KB "
                  f"({r['lp_ip2']['mbps']:.0f}Mbps) enc {r['lp_ip2']['encode_fps']:.1f}fps "
                  f"dec {r['lp_ip2']['decode_fps']:.1f}fps "
                  f"(I={r['lp_ip2']['i_frames']}/{N})")

    path = REPO / "tools" / f"lt_vs_lp_speed_{DATE}.json"
    path.write_text(json.dumps({"date": DATE, "n": N, "lt_bpp": LT_BPP,
                                "lp_qp": LP_QP, "lp_ems": LP_EMS,
                                "layers": results},
                               indent=1))
    print(f"saved -> {path}")


if __name__ == "__main__":
    main()
