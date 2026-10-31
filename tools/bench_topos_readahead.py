#!/usr/bin/env python3
"""Topos 产品解码路径读前批量预取基准（阶段4 兑现的官方复现脚本）。

口径（同窗成对差分，方法学与 bench_out/m11_stage4 一致）：
  off —— ToposMediaSource(read_ahead=0)：逐帧 packet_into + 单帧解码
         （M2 产品路径基线）
  on  —— ToposMediaSource(read_ahead=自动)：顺序确认后 decode_views_batch
         批量解码 + 缓存命中（播放/导出 provider 的真实形态：IO 在路径内，
         消费方逐帧取走即弃、平面池滚动复用）

相邻成对交替测量（~同窗），逐对 fps 比值取中位数——抵消机器慢漂移；
每轮先整遍预热（建池/热页/分支）再 seek(0) 计时 --loops 圈。

默认素材 = 阶段4 基准同文件（与 342.4/429.4 参考同源，hq rate 码流）；
文件缺失时退回确定性合成素材。

用法：
    .venv/bin/python tools/bench_topos_readahead.py [--pairs 12] [--loops 3]
        [--movies PATH [PATH ...]] [--depth N]
"""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

_STAGE4_MOVIES = [
    PROJECT_ROOT / "bench_out" / "topos_vs_ref" / "20260902_065735_2k" / "topos_hq_rate_t8.mov",
    PROJECT_ROOT / "bench_out" / "topos_vs_ref" / "20260902_065819_4k" / "topos_hq_rate_t8.mov",
]


def _make_movie(path: str, w: int, h: int, frames: int, qp: int) -> None:
    """确定性合成素材（梯度 + 帧偏移，10-bit 满刻度 4:2:2）。"""
    import numpy as np
    from src.shared.codec.topos_binding import (
        TOPOS_CODEC_ABI_VERSION,
        ToposCodec,
        ToposMuxFile,
        _CFrameConfig,
    )

    codec = ToposCodec()
    fc = _CFrameConfig()
    fc.struct_size = ctypes.sizeof(_CFrameConfig)
    fc.abi_version = TOPOS_CODEC_ABI_VERSION
    fc.visible_width, fc.visible_height = w, h
    fc.qp_base = qp
    fc.qmatrix_id = 1
    fc.color_range = 1
    fc.color_primaries = 1
    fc.color_transfer = 1
    fc.color_matrix = 1
    fc.sar_num, fc.sar_den = 1, 1
    mc = codec.movie_config(w, h, qp=qp, qmatrix=1)
    cw = (w + 1) // 2

    def plane(width: int, height: int, base: int) -> bytes:
        a = (
            (np.arange(width)[None, :] * 7 + np.arange(height)[:, None] * 13 + base)
            % 512 + 256
        ).astype("<u2")
        return a.tobytes()

    with ToposMuxFile(codec, path, mc) as mux:
        for i in range(frames):
            b = (i * 977) % 2048
            pkt, _ = codec.encode_frame(
                fc, [plane(w, h, 300 + b), plane(cw, h, 480 + b), plane(cw, h, 520 + b)]
            )
            mux.add_packet(pkt, pts=i * 1000, dur=1000)
        mux.finish()


def _run_once(path: str, read_ahead: int | None, loops: int, hold: int = 2) -> float:
    """一轮测量：整遍预热（不计入）→ seek(0) → loops 圈全读计时，返回 fps。

    hold = 消费方在飞持有帧数（deque maxlen，默认 2——播放/导出 worker
    解码超前 + 渲染排队的稳态引用形态；平面池须在持有下滚动复用）。
    """
    from collections import deque

    from src.shared.media.topos_source import ToposMediaSource

    src = ToposMediaSource(path, read_ahead=read_ahead)
    src.open()
    try:
        total = src.total_frames
        q = deque(maxlen=hold)
        for _ in range(total):  # 预热遍：建池/热页/分支（稳态口径）
            q.append(src.read_next_frame())
        q.clear()
        t0 = time.perf_counter()
        for _ in range(loops):
            src.seek_frame(0)
            for _ in range(total):
                q.append(src.read_next_frame())
        dt = time.perf_counter() - t0
    finally:
        src.close()
    return (total * loops) / dt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--movies", nargs="*", default=None,
                    help="Topos MOV 路径列表（缺省=阶段4 同源素材，缺失退回合成）")
    ap.add_argument("--pairs", type=int, default=12, help="成对差分对数")
    ap.add_argument("--loops", type=int, default=3, help="每轮计时圈数")
    ap.add_argument("--depth", type=int, default=None,
                    help="on 侧深度覆盖（缺省=自动）")
    args = ap.parse_args()

    movies: list[str] = []
    if args.movies:
        movies = [str(Path(p)) for p in args.movies]
    else:
        for p in _STAGE4_MOVIES:
            if p.is_file():
                movies.append(str(p))
        if not movies:
            movies = [str(PROJECT_ROOT / "bench_out" / "readahead" / "synth.mov")]
            Path(movies[0]).parent.mkdir(parents=True, exist_ok=True)
            print("阶段4 素材缺失，生成合成素材（与参考数字不可比）…")
            _make_movie(movies[0], 2048, 1080, 48, 28)

    from src.shared.media.topos_source import ToposMediaSource

    overall: list[tuple[str, float, float]] = []
    for path in movies:
        probe = ToposMediaSource(path)
        probe.open()
        try:
            geo = f"{probe.width}x{probe.height}×{probe.total_frames}f"
        finally:
            probe.close()

        # 预热进程级（库加载/单例）后成对交替
        _run_once(path, 0, args.loops)
        ratios: list[float] = []
        off_hist, on_hist = [], []
        for _ in range(args.pairs):
            off = _run_once(path, 0, args.loops)
            on = _run_once(path, args.depth, args.loops)
            off_hist.append(off)
            on_hist.append(on)
            ratios.append(on / off)

        med = statistics.median(ratios)
        srt = sorted(ratios)
        q1 = srt[len(srt) // 4] - 1.0
        q3 = srt[(3 * len(srt)) // 4] - 1.0
        pos = sum(1 for r in ratios if r > 1.0)
        print(f"[{geo}] {Path(path).name} depth={'自动' if args.depth is None else args.depth}")
        print(
            f"  off(逐帧): median {statistics.median(off_hist):7.1f} fps | "
            f"on(读前批量): median {statistics.median(on_hist):7.1f} fps"
        )
        print(
            f"  成对差分（{args.pairs} 对，{pos} 对为正）：median {med - 1:+.1%}  "
            f"IQR [{q1:+.1%}, {q3:+.1%}]"
        )
        overall.append((geo, statistics.median(off_hist), statistics.median(on_hist)))

    print("\n汇总：")
    for geo, off, on in overall:
        print(f"  {geo}: {off:.1f} → {on:.1f} fps（{(on / off - 1):+.1%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
