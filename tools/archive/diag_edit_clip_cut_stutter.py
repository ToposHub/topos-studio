#!/usr/bin/env python3
"""诊断：编辑（时间线）页播放越过剪辑点时的 1-2 帧顿挫。

复刻 app 的真实播放解码行为（EditGpuDecodeManager + per-clip stream）：

- 编辑页每个 timeline clip 有独立解码流（stream_key=clip_id）。
- 播放开始时只 warmup 当前 plan 里的 clip source；
  `_schedule_playback_prebuffer` 已被禁用（no-op），播放途中不做任何预解码。
- worker 每帧调用 manager.get_frame(asset, source_frame, target_size,
  stream_key=clip_id)。

场景 A（当前 app 行为）：只 warmup clip0，播放到 clip1/clip2 边界时，
    新流现场创建 + 从 cursor 0 seek/decode-forward 到边界源帧。
场景 B：播放开始时 warmup 所有 clip 的 source（但 cursor 仍在 0）。
场景 C：场景 B + 到达边界前把下一 clip 的流预定位到其起始帧（后台）。
场景 D：只 warmup clip0 + 预定位（与修复后 app 行为一致：预定位负责创建后续流）。

用法：
  .venv/bin/python tools/diag_edit_clip_cut_stutter.py \
      --file "tests/test_videos/2K_h264.mp4" --clip-frames 36 --clips 3 \
      [--scenario A|B|C|all] [--reordered]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TARGET_SIZE = (1920, 1080)  # 与 app _decode_target_size 同一数量级


def build_clips(clip_frames: int, num_clips: int, reordered: bool):
    """返回 [(clip_id, source_start, source_end)]（inclusive）。"""
    clips = []
    for i in range(num_clips):
        start = i * clip_frames
        end = start + clip_frames - 1
        clips.append((f"clip-{i}", start, end))
    if reordered:
        # 乱序：时间线顺序对应的源范围不再是升序
        clips = [clips[0], clips[2], clips[1]] if num_clips >= 3 else clips[::-1]
    return clips


def run_scenario(
    path: str,
    clips,
    scenario: str,
    fps: float,
) -> dict:
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    manager = EditGpuDecodeManager()
    frame_period = 1.0 / fps

    # ── 播放开始时的 warmup（复刻 _warmup_gpu_decode_sources：只暖当前 plan）──
    if scenario in ("A", "D"):
        manager.warmup_source(path, target_size=TARGET_SIZE, stream_key=clips[0][0])
    else:
        for clip_id, _, _ in clips:
            manager.warmup_source(path, target_size=TARGET_SIZE, stream_key=clip_id)
    time.sleep(0.8)  # 等待后台 warmup 完成

    boundary_frames = {}  # clip_idx(前一个clip) -> 进入下一clip的timeline帧
    acc = 0
    for i, (_, s, e) in enumerate(clips):
        if i > 0:
            boundary_frames[i - 1] = acc
        acc += e - s + 1
    records = []  # (timeline_frame, clip_idx, source_frame, elapsed_ms)

    # 场景 C：后台预定位线程——播放接近边界时把下一 clip 流预定位到起点
    prepos_done = set()
    stop_flag = [False]

    def _prepos_worker(current_tl: list):
        while not stop_flag[0]:
            tl = current_tl[0]
            for ci in range(len(clips) - 1):
                if ci in prepos_done:
                    continue
                # 距离下一 clip 边界 <=8 帧时预定位
                boundary_tl = boundary_frames.get(ci)
                if boundary_tl is None or tl < boundary_tl - 8 or tl >= boundary_tl:
                    continue
                next_clip_id, next_start, _ = clips[ci + 1]
                manager.preposition_stream_frame(
                    path, next_start, target_size=TARGET_SIZE,
                    stream_key=next_clip_id,
                )
                prepos_done.add(ci)
            time.sleep(0.002)

    current_tl = [0]
    prepos_thread = None
    if scenario in ("C", "D"):
        prepos_thread = threading.Thread(
            target=_prepos_worker, args=(current_tl,), daemon=True
        )
        prepos_thread.start()

    total_timeline_frames = sum(e - s + 1 for _, s, e in clips)
    clip_idx = 0
    t_play_start = time.perf_counter()

    for timeline_frame in range(total_timeline_frames):
        current_tl[0] = timeline_frame
        # 当前 timeline 帧属于哪个 clip
        acc = 0
        ci = 0
        for i, (_, s, e) in enumerate(clips):
            dur = e - s + 1
            if timeline_frame < acc + dur:
                ci = i
                break
            acc += dur
        clip_id, s, e = clips[ci]
        source_frame = s + (timeline_frame - acc)

        t0 = time.perf_counter()
        frame, frame_type = manager.get_frame(
            path, source_frame, target_size=TARGET_SIZE, stream_key=clip_id
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ok = frame is not None
        got_idx = getattr(frame, "frame_index", None)
        records.append((timeline_frame, ci, source_frame, elapsed_ms, ok, got_idx, frame_type))

        # 模拟播放节拍：解码快于帧周期则等待到下一帧时刻
        target = t_play_start + (timeline_frame + 1) * frame_period
        remain = target - time.perf_counter()
        if remain > 0:
            time.sleep(remain)

    stop_flag[0] = True
    if prepos_thread is not None:
        prepos_thread.join(timeout=1.0)

    # ── 分析 ──
    period_ms = frame_period * 1000.0
    boundary_tl_set = {boundary_frames[k] for k in boundary_frames}
    print(f"\n===== 场景 {scenario} | {os.path.basename(path)} =====")
    print(f"帧周期 {period_ms:.1f}ms | clips={[(cid, s, e) for cid, s, e in clips]}")
    print(f"{'tl帧':>5} {'clip':>4} {'源帧':>6} {'耗时ms':>8}  标记")
    stutters = 0
    for tl, ci, sf, ms, ok, got_idx, ft in records:
        flags = []
        if tl in boundary_tl_set:
            flags.append("<<< 剪辑边界(进入 " + clips[ci][0] + ")")
        if ms > period_ms:
            flags.append(f"卡顿({ms / period_ms:.1f}帧周期)")
            stutters += 1
        if not ok:
            flags.append("解码失败!")
        elif got_idx is not None and got_idx != sf:
            flags.append(f"错帧!要{sf}得{got_idx}")
        if flags or tl in boundary_tl_set or ms > period_ms * 0.8:
            print(f"{tl:>5} {ci:>4} {sf:>6} {ms:>8.1f}  {' '.join(flags)}")
    boundary_ms = [ms for tl, ci, sf, ms, *_ in records if tl in boundary_tl_set]
    inside_ms = [
        ms for tl, ci, sf, ms, *_ in records
        if tl not in boundary_tl_set and tl > 2
    ]
    print(f"边界帧耗时: {[f'{m:.1f}' for m in boundary_ms]}")
    print(
        f"非边界平均耗时: {sum(inside_ms) / max(1, len(inside_ms)):.1f}ms, "
        f"卡顿帧(>1帧周期): {stutters}"
    )
    try:
        manager.close_all()
    except Exception:
        pass
    return {"stutters": stutters, "boundary_ms": boundary_ms}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="tests/test_videos/2K_h264.mp4")
    ap.add_argument("--clip-frames", type=int, default=36)
    ap.add_argument("--clips", type=int, default=3)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--scenario", default="all", choices=["A", "B", "C", "D", "all"])
    ap.add_argument("--reordered", action="store_true")
    args = ap.parse_args()

    clips = build_clips(args.clip_frames, args.clips, args.reordered)
    scenarios = ["A", "B", "C", "D"] if args.scenario == "all" else [args.scenario]
    results = {}
    for sc in scenarios:
        results[sc] = run_scenario(args.file, clips, sc, args.fps)
    print("\n===== 汇总 =====")
    for sc, r in results.items():
        b = ", ".join(f"{m:.1f}ms" for m in r["boundary_ms"]) or "-"
        print(f"场景 {sc}: 卡顿帧={r['stutters']}, 边界帧耗时=[{b}]")


if __name__ == "__main__":
    main()
