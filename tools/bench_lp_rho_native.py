#!/usr/bin/env python3
"""V9 批 6：native ρ 复测——用真实现（ToposGopContext IP-2 链）复核批 0
spike 的 ρ 表（计划 §5 批 2 点名义务 + §7 出货门评审输入）。

与 bench_lp_spike.py 的差异：P 帧走 V9 原生编码（残差伪图 + GOP context
事务 + 「P ≥ 最近 I 回退」自动决策）——intra 通路代理的局限（批 0 报告
§7 登记）在此消除。内容层窗口与批 0 相同（tools/lp_spike_2026-09-14.json）。

输出 tools/lp_rho_native_<date>.json + stdout 摘要。
"""
from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.shared.codec import topos_binding as tb  # noqa: E402

SRC_4K = REPO / "tests/test_videos/4K_prores422HQ.mov"
SRC_2K = REPO / "tests/test_videos/2K_prores422HQ.mov"
QPS = [64, 72]
N = 8                      # 每层帧数（IP-2 口径：交替 I/P）
MID = 512
DATE = "2026-09-14"

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


def extract(src: Path, start: int, n: int) -> list[list[np.ndarray]]:
    import av
    cont = av.open(str(src))
    frames = []
    for i, f in enumerate(cont.decode(video=0)):
        if i >= start:
            frames.append(_planes(f))
            if len(frames) >= n:
                break
    cont.close()
    assert len(frames) == n
    return frames


def v9_cfg(w: int, h: int, qp: int) -> tb._CFrameConfig:
    cfg = tb._CFrameConfig()
    cfg.struct_size = ctypes.sizeof(tb._CFrameConfig)
    cfg.abi_version = tb.TOPOS_CODEC_ABI_VERSION
    cfg.visible_width, cfg.visible_height = w, h
    cfg.profile, cfg.pixel_format, cfg.bit_depth = 3, 0, 10
    cfg.qmatrix_id, cfg.qp_base = 0, qp
    cfg.color_range = cfg.color_primaries = cfg.color_transfer = 1
    cfg.color_matrix = 1
    cfg.reserved[0] = 10
    return cfg


def run_layer(tag: str, src: Path, start: int, qp: int) -> dict:
    frames = extract(src, start, N)
    h, w = frames[0][0].shape
    frames = [[p.astype(np.uint16) for p in f] for f in frames]
    sizes = []
    types = []
    with tb.ToposGopContext(tb.ToposCodec(), v9_cfg(w, h, qp)) as ctx:
        for k, planes in enumerate(frames):
            pkt, st = ctx.encode_frame(planes, force_intra=(k == 0))
            sizes.append((st.packet_size, pkt[15]))
            types.append(int(pkt[15]))
    i_sizes = [s for s, t in sizes if t == 0]
    p_sizes = [s for s, t in sizes if t == 1]
    fallbacks = sum(1 for k, t in enumerate(types) if k > 0 and t == 0)
    # IP-2 有效体积（回退帧按 I 计——已是事实）vs 全 I 体积
    total_ip = sum(s for s, _ in sizes)
    total_intra = sum(s for s, t in sizes if t == 0) * 0  # 精确账见下
    # 与「全 I 基线」同帧数对比：每帧若全 I，体积 = I 候选；无独立测量时
    # 用帧 0 的 I 代表（内容同层，误差有限）——报告同时给出逐帧 ρ。
    base_i = i_sizes[0] if i_sizes else sizes[0][0]
    rho_effective = total_ip / (base_i * N)
    return {
        "frame_bytes": sizes,
        "frame_types": types,
        "fallbacks_after_first": fallbacks,
        "ip2_total_bytes": total_ip,
        "all_intra_est_bytes": base_i * N,
        "gain_vs_all_intra_est": round(1.0 - rho_effective, 4),
        "rho_p_frames": [round(s / base_i, 4) for s, t in sizes if t == 1],
    }


def main() -> None:
    out = {"date": DATE, "qps": QPS, "n_frames": N,
           "method": "native ToposGopContext IP-2 (P>=lastI auto fallback)",
           "layers": {}}
    for tag, src, start in LAYERS:
        lay = {}
        for qp in QPS:
            lay[str(qp)] = run_layer(tag, src, start, qp)
            r = lay[str(qp)]
            print(f"{tag} qp{qp}: types={''.join(str(t) for t in r['frame_types'])} "
                  f"gain_vs_allI={r['gain_vs_all_intra_est'] * 100:+.1f}% "
                  f"rho_p={r['rho_p_frames']}")
        out["layers"][tag] = lay
    dst = REPO / f"tools/lp_rho_native_{DATE}.json"
    dst.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(f"→ {dst}")


if __name__ == "__main__":
    main()
