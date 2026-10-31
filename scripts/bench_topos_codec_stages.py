"""Topos 编解码分阶段微基准：裸解码 / 裸编码 / 纯转码循环 / 产物回解码。

目的：把时间线直通导出（``topos_direct_planar_cpu_encode``）拆成
"codec 本体"与"时间线管线开销"两段，回答导出速度还有多少提升空间。

阶段定义（全部走生产同一代码面，非 native 原型工具）：
  1) decode_only : ToposMediaSource 顺序 read_frame 全部帧（读时间线素材
     的真实解码速度）；
  2) encode_only : 预解码的帧轮转喂 ToposVideoEncoder（单档位纯编码速度，
     解码为零）；
  3) transcode   : read_frame → encode_frame 流式循环（与直通导出同形，
     无进度回调/telemetry/暂停检查）；
  4) decode_out  : 对 3) 产物文件再跑 1)（导出产物的回读解码速度）。

用法：
    .venv/bin/python -u scripts/bench_topos_codec_stages.py
    .venv/bin/python -u scripts/bench_topos_codec_stages.py \
        --source 4K_topos_hq.mov --tiers proxy,hq
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_VIDEOS_DIR = ROOT / "tests" / "test_videos"


def probe_source(path: Path) -> Dict[str, Any]:
    from src.shared.media.factory import MediaSourceFactory

    probe = MediaSourceFactory.probe_video_metadata_cached(str(path))
    vm = probe.metadata
    fps = getattr(vm, "fps", None)
    return {
        "codec": vm.codec_name, "pix_fmt": vm.pixel_format,
        "width": int(vm.width), "height": int(vm.height),
        "fps": float(fps.num) / float(fps.den) if fps else 25.0,
        "total_frames": int(vm.total_frames or 0),
    }


def bench_decode(path: Path, frames: int, passes: int) -> Dict[str, Any]:
    """阶段 1：顺序解码整个文件。返回每 pass fps。"""
    from src.shared.media.topos_source import ToposMediaSource

    fps_list: List[float] = []
    ms_per_frame_last: List[float] = []
    for _ in range(passes):
        src = ToposMediaSource(str(path))
        src.open()
        t0 = time.perf_counter()
        n = 0
        try:
            for i in range(frames):
                f = src.read_frame(i)
                if f is None:
                    break
                n += 1
        finally:
            src.close()
        wall = time.perf_counter() - t0
        fps_list.append(n / wall if wall > 0 else 0.0)
        ms_per_frame_last.append(wall * 1000.0 / max(1, n))
    return {"fps_list": fps_list, "ms_per_frame": ms_per_frame_last}


def make_encoder_config(meta: Dict[str, Any], tier: str) -> SimpleNamespace:
    from src.features.edit.core.timeline_v2 import Rational

    fps_i = max(1, int(round(meta["fps"])))
    return SimpleNamespace(
        profile=tier, pix_fmt="yuv422p10le",
        width=meta["width"], height=meta["height"],
        fps=Rational(fps_i, 1), crf=None, has_alpha=False,
    )


def bench_encode(frames_pool: List[Any], count: int, tier: str,
                 meta: Dict[str, Any], out_path: Path, passes: int,
                 slice_threads: int) -> Dict[str, Any]:
    """阶段 2：预解码帧池轮转喂编码器（纯编码，无解码）。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder

    fps_list: List[float] = []
    size_bytes = 0
    for p in range(passes):
        enc = ToposVideoEncoder(
            str(out_path), make_encoder_config(meta, tier),
            slice_threads=slice_threads)
        enc.open()
        t0 = time.perf_counter()
        n = 0
        try:
            for i in range(count):
                enc.encode_frame(frames_pool[i % len(frames_pool)])
                n += 1
        finally:
            enc.close()
        wall = time.perf_counter() - t0
        fps_list.append(n / wall if wall > 0 else 0.0)
        size_bytes = out_path.stat().st_size
    return {"fps_list": fps_list, "output_bytes": size_bytes}


def bench_transcode(path: Path, tier: str, meta: Dict[str, Any],
                    frames: int, out_path: Path, passes: int,
                    slice_threads: int) -> Dict[str, Any]:
    """阶段 3：read_frame → encode_frame 流式循环（直通导出同形）。"""
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.topos_source import ToposMediaSource

    fps_list: List[float] = []
    dec_ms: List[float] = []
    enc_ms: List[float] = []
    for _ in range(passes):
        enc = ToposVideoEncoder(
            str(out_path), make_encoder_config(meta, tier),
            slice_threads=slice_threads)
        enc.open()
        src = ToposMediaSource(str(path))
        src.open()
        t0 = time.perf_counter()
        d_acc = 0.0
        e_acc = 0.0
        try:
            for i in range(frames):
                ta = time.perf_counter()
                decoded = src.read_frame(i)
                d_acc += time.perf_counter() - ta
                if decoded is None:
                    break
                tb = time.perf_counter()
                enc.encode_frame(decoded)
                e_acc += time.perf_counter() - tb
        finally:
            src.close()
            enc.close()
        wall = time.perf_counter() - t0
        fps_list.append(frames / wall if wall > 0 else 0.0)
        dec_ms.append(d_acc * 1000.0 / max(1, frames))
        enc_ms.append(e_acc * 1000.0 / max(1, frames))
    return {
        "fps_list": fps_list, "decode_ms_per_frame": dec_ms,
        "encode_ms_per_frame": enc_ms,
        "output_bytes": out_path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="2K_topos_hq.mov")
    parser.add_argument("--tiers", default="proxy,hq")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    try:
        from src.features.edit.core.timeline_export import (
            _topos_timeline_cpu_threads,
        )
        enc_threads, _dec = _topos_timeline_cpu_threads()
    except Exception:
        enc_threads = min(16, __import__("os").cpu_count() or 1)

    source = TEST_VIDEOS_DIR / args.source
    if not source.exists():
        print(f"❌ 素材不存在: {source}")
        return 1
    meta = probe_source(source)
    frames = meta["total_frames"]
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else (
        ROOT / "bench_out" / "topos_codec_stages" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 76)
    print("Topos 编解码分阶段微基准（生产绑定路径，非 native 原型）")
    print("=" * 76)
    print(f"素材: {source.name}  {meta['width']}x{meta['height']}@{meta['fps']:.0f}"
          f"  {meta['pix_fmt']}  {frames} 帧 × {args.passes} passes"
          f"  编码线程={enc_threads}")
    print()

    # 预解码帧池（裸编码用；4K 8 帧 ≈ 264MB，内存安全）
    from src.shared.media.topos_source import ToposMediaSource

    pool_n = min(frames, 8)
    pool_src = ToposMediaSource(str(source))
    pool_src.open()
    frames_pool = [pool_src.read_frame(i) for i in range(pool_n)]
    pool_src.close()

    results: Dict[str, Any] = {
        "source": str(source), "source_meta": meta, "frames": frames,
        "passes": args.passes, "encode_threads": enc_threads, "tiers": {},
    }

    dec = bench_decode(source, frames, args.passes)
    dec_med = statistics.median(dec["fps_list"])
    print(f"[decode_only] 裸解码（读 {source.name}）: "
          f"中位 {dec_med:.1f} fps  "
          f"各 pass {[round(x, 1) for x in dec['fps_list']]}")
    results["decode_only"] = dec

    for tier in tiers:
        print("-" * 76)
        enc = bench_encode(frames_pool, frames, tier, meta,
                           out_dir / f"enc_{tier}.mov", args.passes,
                           enc_threads)
        enc_med = statistics.median(enc["fps_list"])
        mbps = enc["output_bytes"] * 8 / frames / meta["fps"] / 1e6
        print(f"[encode_only/{tier}] 裸编码: 中位 {enc_med:.1f} fps  "
              f"各 pass {[round(x, 1) for x in enc['fps_list']]}  "
              f"输出 {enc['output_bytes']/(1024*1024):.1f}MB (~{mbps:.0f} Mbps)")
        tc = bench_transcode(source, tier, meta, frames,
                             out_dir / f"tc_{tier}.mov", args.passes,
                             enc_threads)
        tc_med = statistics.median(tc["fps_list"])
        print(f"[transcode/{tier}] 解码→编码流式: 中位 {tc_med:.1f} fps  "
              f"各 pass {[round(x, 1) for x in tc['fps_list']]}  "
              f"逐帧 decode {statistics.median(tc['decode_ms_per_frame']):.2f}ms"
              f" + encode {statistics.median(tc['encode_ms_per_frame']):.2f}ms")
        dout = bench_decode(out_dir / f"tc_{tier}.mov", frames, args.passes)
        dout_med = statistics.median(dout["fps_list"])
        print(f"[decode_out/{tier}] 产物回解码: 中位 {dout_med:.1f} fps  "
              f"各 pass {[round(x, 1) for x in dout['fps_list']]}")
        results["tiers"][tier] = {
            "encode_only": enc, "transcode": tc, "decode_out": dout,
            "encode_only_fps_med": enc_med, "transcode_fps_med": tc_med,
            "decode_out_fps_med": dout_med,
        }

    print("=" * 76)
    print("汇总（中位 fps）:")
    print(f"  裸解码（topos HQ 素材）        : {dec_med:8.1f} fps")
    for tier in tiers:
        t = results["tiers"][tier]
        print(f"  裸编码 → {tier:8s}          : "
              f"{t['encode_only_fps_med']:8.1f} fps")
        print(f"  解码→编码转码 → {tier:8s}    : "
              f"{t['transcode_fps_med']:8.1f} fps")
        print(f"  产物回解码（{tier}）          : "
              f"{t['decode_out_fps_med']:8.1f} fps")

    result_path = out_dir / "results.json"
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n结果已保存: {result_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
