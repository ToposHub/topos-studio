"""合成页 video_frame 节点播放性能基准（无头，真实素材）。

模拟 CompositePage 的独立 comp clip 播放循环：
  QTimer tick (1000/fps ms)
    → _do_evaluate(eval_ctx.frame_number=playhead)
    → cache miss 时持帧 + 后台解码
    → 解码完成 notify_frame_ready → 16ms 防抖后再求值（第二次渲染）
统计墙上时钟内真正上屏的帧数（displayed fps）与各环节耗时。

用法：
  .venv/bin/python tools/bench_composite_playback.py [视频路径 ...]
"""
from __future__ import annotations

import os
import sys
import time
import logging
import threading

# QA-03（2026-10-14 审计）：以 __file__ 定位本 checkout——旧硬编码
# topos-color01，在 topos-color02 里执行会测错代码树。
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(level=logging.WARNING)

if os.environ.get("TC_BENCH_TRACE_LEAK"):
    import traceback
    import src.shared.media.gpu_frame as _gf
    _orig_init = _gf.GpuDecodedFrame.__init__
    _stacks: dict = {}
    def _traced_init(self, *a, **k):
        _orig_init(self, *a, **k)
        _stacks[id(self)] = traceback.extract_stack()[-8:-1]
    _gf.GpuDecodedFrame.__init__ = _traced_init
    _orig_del = _gf.GpuDecodedFrame.__del__
    def _traced_del(self):
        if (not hasattr(self, "release") or not self.release.is_released) and id(self) in _stacks:
            print("=== LEAK 追踪 ===", flush=True)
            for _line in traceback.format_list(_stacks[id(self)]):
                print(_line.rstrip(), flush=True)
        _stacks.pop(id(self), None)
        _orig_del(self)
    _gf.GpuDecodedFrame.__del__ = _traced_del

import numpy as np
import moderngl

from src.features.composite.core.eval_context import CompositeEvalContext


class _NotifyHub:
    """模拟 VideoContext.notify_frame_ready → 页面重求值（16ms 防抖）。"""

    def __init__(self) -> None:
        self.pending = threading.Event()
        self.count = 0

    def __call__(self) -> None:
        self.count += 1
        self.pending.set()


def run_video(path: str, duration_s: float = 6.0) -> None:
    from src.features.composite.nodes.video_frame_node import VideoFrameNode

    print(f"\n=== {path.split('/')[-1]} ===")
    node = VideoFrameNode("bench-video")
    node.parameters["file_path"] = path
    if not node._open_video(path):
        print("  open failed:", node.error_message)
        return

    meta = node.video_metadata
    fps = meta.fps.to_fps() if meta else node.parameters.get("fps", 24.0)
    total = node.total_frames
    print(f"  {node.parameters['video_width']}x{node.parameters['video_height']} "
          f"@{fps:.2f}fps, {total} frames, clip_id={node._clip_id}")

    gl = moderngl.create_standalone_context()
    notify_hub = _NotifyHub()
    # 注入真实页面同款回调：VideoContext.notify_frame_ready → 页面重求值
    node._video_context.set_frame_ready_handler(notify_hub)

    eval_ctx = CompositeEvalContext(gl=gl)

    tick_interval = 1.0 / fps
    t_end = time.perf_counter() + duration_s
    playhead = 0
    displayed: list[tuple[float, int]] = []  # (wall_time, frame)
    last_shown = -1
    eval_count = 0
    upload_times: list[float] = []

    # 主循环：同时驱动 tick 求值与 notify 后的重求值（模拟 UI 线程防抖合并）
    next_tick = time.perf_counter()
    reeval_at: float | None = None
    while True:
        now = time.perf_counter()
        if now >= t_end:
            break

        # 播放 tick：按墙钟推进 playhead（与 _on_comp_playback_tick 一致）
        if now >= next_tick:
            eval_ctx.frame_number = playhead
            t0 = time.perf_counter()
            node._do_evaluate(inputs={}, ctx=eval_ctx, fb_manager=None,
                              resource_pool=None, render_mode="preview")
            eval_count += 1
            if node._current_frame != last_shown and node.is_ready:
                displayed.append((now, node._current_frame))
                last_shown = node._current_frame
            if playhead % 25 == 0 and os.environ.get("TC_BENCH_PROBE"):
                with node._cache_lock:
                    ncache = len(node._frame_cache)
                print(f"    [probe] playhead={playhead} current={node._current_frame} "
                      f"ready={node.is_ready} state={node._load_state} cache={ncache}")
            playhead += 1
            if playhead >= total:
                playhead = total - 1
            next_tick += tick_interval

        # notify 到达 → 立即重求值（与真实页面 _evaluate_graph_now 一致；
        # 渲染线程对 EVALUATE_GRAPH 有单飞合并）
        if notify_hub.pending.is_set() and reeval_at is None:
            notify_hub.pending.clear()
            reeval_at = now
        if reeval_at is not None and now >= reeval_at:
            eval_ctx.frame_number = playhead - 1  # 防抖期间 playhead 未走下一 tick
            t0 = time.perf_counter()
            node._do_evaluate(inputs={}, ctx=eval_ctx, fb_manager=None,
                              resource_pool=None, render_mode="preview")
            upload_times.append(time.perf_counter() - t0)
            eval_count += 1
            if node._current_frame != last_shown and node.is_ready:
                displayed.append((now, node._current_frame))
                last_shown = node._current_frame
            reeval_at = None

        time.sleep(0.001)

    # 统计
    wall = duration_s
    n = len(displayed)
    distinct = len({f for _, f in displayed})
    print(f"  playhead ticks: ~{int(wall * fps)} | graph evaluations: {eval_count}")
    print(f"  displayed frames: {n} ({n / wall:.1f} fps), distinct frames: {distinct}")
    if len(displayed) >= 2:
        gaps = [displayed[i + 1][0] - displayed[i][0] for i in range(len(displayed) - 1)]
        gaps.sort()
        print(f"  display interval ms: median={gaps[len(gaps) // 2] * 1000:.0f} "
              f"p90={gaps[int(len(gaps) * 0.9)] * 1000:.0f} max={gaps[-1] * 1000:.0f}")
    if upload_times:
        print(f"  cache-hit evaluate ms: median={sorted(upload_times)[len(upload_times) // 2] * 1000:.0f}")

    node._stop_preload_thread(join=True)
    node.cleanup() if hasattr(node, "cleanup") else None
    gl.release()


def micro_decode(path: str, frames: int = 5) -> None:
    """单帧解码+转换微基准：量化 service preview 路径的每帧成本。"""
    from src.features.composite.nodes.video_frame_node import VideoFrameNode

    node = VideoFrameNode("bench-micro")
    node.parameters["file_path"] = path
    if not node._open_video(path):
        return
    times = []
    for f in range(10, 10 + frames):
        t0 = time.perf_counter()
        data = node._decode_frame(f, "preview")
        times.append((time.perf_counter() - t0) * 1000)
        # VT 延迟导入帧持有 CVPixelBuffer，必须显式释放
        if data is not None and hasattr(data, "gpu_frame"):
            from src.shared.media.gpu_frame import safe_release_gpu_frame
            safe_release_gpu_frame(data.gpu_frame)
        del data
    node._stop_preload_thread(join=True)
    name = path.split("/")[-1]
    print(f"  [micro] {name}: decode+convert per frame ms: "
          + ", ".join(f"{t:.0f}" for t in times))


if __name__ == "__main__":
    import glob
    # QA-03：媒体根目录可用环境变量覆盖；默认定位本 checkout。
    root = os.environ.get("TC_BENCH_MEDIA_ROOT",
                          os.path.join(_REPO_ROOT, "tests", "test_videos"))
    paths = sys.argv[1:] or sorted(glob.glob(f"{root}/*.mov") + glob.glob(f"{root}/*.mp4"))
    # 微基准先跑一个 2K H264（最常见）和一个 4K ProRes（最重）
    micro_decode(f"{root}/2K_h264.mov" if __import__("os").path.exists(f"{root}/2K_h264.mov")
                 else paths[0])
    micro_decode(f"{root}/4K_prores422HQ.mov")
    for p in paths:
        run_video(p)
