#!/usr/bin/env python3
"""解析 TOPOS 语料包的真实编码 qp 分布（帧头 qp_base + V8 段目录 qp_delta）。

帧头 53B：qp_base = data[29]（frame_header.c out[29]）。
V8 布局（packet.h §2.2）：53B 帧头 + 8B 扩展头（[0]=segment_blocks_log2,
[1]=tile_rows_log2）+ 每平面 [S_p×12B 段目录（off u32|len u32|qp_delta_biased
u8|res u8×3）+ …]。有效 qp = qp_base + (qp_delta_biased − 64) ∈ 0..95。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import av

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposMovieFile,
)


def plane_block_dims(w: int, h: int, subsampling: str) -> list[tuple[int, int]]:
    """每平面 8×8 块数 (cols, rows)。422: U/V 半宽；444: 同尺寸。"""
    y = (w // 8, h // 8)
    if subsampling == "444":
        return [y, y, y]
    return [y, (w // 16, h // 8), (w // 16, h // 8)]


def parse_frame_qps(data: memoryview, size: int, dims: list[tuple[int, int]]) -> dict:
    d = bytes(data[:size])
    assert d[0:4] == b"TPIC", "not a topos frame"
    qp_base = d[29]
    major = d[6]
    qps = [qp_base]
    if major >= 8:
        seg_blocks_log2 = d[53]
        seg_blocks = 1 << seg_blocks_log2
        off = 53 + 8
        for cols, rows in dims:
            n_blocks = cols * rows
            s_p = (n_blocks + seg_blocks - 1) // seg_blocks
            for i in range(s_p):
                e = off + i * 12
                qps.append(qp_base + (d[e + 8] - 64))
            off += s_p * 12  # 目录之后还有瓦片表/码流区，但我们只走目录
    else:
        # V7：逐 slice「17B 头 + payload」交错排列（codec.c emit_done 路径）
        off = 53
        plane_count = d[27]
        while off + 17 <= len(d):
            payload = int.from_bytes(d[off:off + 4], "big")
            plane = d[off + 4]
            h = int.from_bytes(d[off + 7:off + 9], "big")
            if plane >= plane_count or h == 0:
                break  # 结构不合法 → 视为包尾
            qps.append(qp_base + (d[off + 9] - 64))
            off += 17 + payload
    return {"major": major, "qp_base": qp_base, "qps": qps}


def main() -> int:
    media = REPO / "tests" / "test_videos"
    codec = ToposCodec()
    try:
        for name in sys.argv[1:] or [
            "2K_topos_422_Proxy.mov", "2K_topos_422_LT.mov",
            "2K_topos_422.mov", "2K_topos_422_HQ.mov",
            "4K_topos_422_Proxy.mov", "4K_topos_422.mov",
        ]:
            path = media / name
            movie = ToposMovieFile(codec, str(path))
            info = movie.info()
            n = int(info.sample_count)
            w, h = int(info.width), int(info.height)
            sub = "444" if "444" in name else "422"
            dims = plane_block_dims(w, h, sub)
            hist: Counter[int] = Counter()
            frames = list(range(0, n, 60)) + [n - 1]
            major = None
            base_hist: Counter[int] = Counter()
            for i in frames:
                buf, size = movie.packet_into(i)
                r = parse_frame_qps(buf, size, dims)
                major = r["major"]
                base_hist[r["qp_base"]] += 1
                hist.update(r["qps"])
            movie.close()
            lo, hi = min(hist), max(hist)
            uniq = sorted(set(hist) - set(base_hist))
            print(f"{name}: major={major} frames={n} {w}x{h} sampled={len(frames)}")
            print(f"   per-frame qp_base range {min(base_hist)}..{max(base_hist)}: "
                  f"{dict(sorted(base_hist.items()))}")
            print(f"   per-slice effective qp range {lo}..{hi}; "
                  f"slice deltas != base: {uniq or '无（全帧单一 qp）'}")
    finally:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
