"""合成页 video_frame 节点拖拽/逐帧预览基准（无头，真实素材）。

模拟用户在合成页拖动播放头：
  每个拖拽事件 → set frame → 求值一次（miss 时持帧 + 后台解码）
  → notify 到达 → 立即重求值（与真实页面一致）
统计：
  - settle 延迟：请求某帧到该帧真正上屏的耗时
  - 上屏率：拖拽期间每秒上屏帧数（拖拽跟手度）
  - 结尾定格帧的正确性（停止拖拽后画面是否停在准确帧）

用法：
  .venv/bin/python tools/bench_composite_scrub.py [视频路径 ...]
"""
from __future__ import annotations

import os
import sys
import time
import logging

# QA-03（2026-10-14 审计）：以 __file__ 定位本 checkout（见 bench_composite_playback.py）。
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(level=logging.WARNING)

import numpy as np
import moderngl

from src.features.composite.core.eval_context import CompositeEvalContext


class _Notify:
    def __init__(self) -> None:
        self.pending = False

    def __call__(self) -> None:
        self.pending = True


def run_scrub(path: str, name: str) -> None:
    from src.features.composite.nodes.video_frame_node import VideoFrameNode

    node = VideoFrameNode("scrub")
    node.parameters["file_path"] = path
    if not node._open_video(path):
        print(f"open failed: {path}")
        return
    gl = moderngl.create_standalone_context()
    notify = _Notify()
    node._video_context.set_frame_ready_handler(notify)
    ec = CompositeEvalContext(gl=gl)

    total = node.total_frames
    results = {}

    def eval_at(frame: int) -> None:
        ec.frame_number = frame
        node._do_evaluate(inputs={}, ctx=ec, fb_manager=None,
                          resource_pool=None, render_mode="preview")

    def drain_until(target: int, deadline: float) -> float:
        """等待 target 上屏；返回 settle 耗时（超时返回 -1）。"""
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < deadline:
            if notify.pending:
                notify.pending = False
                eval_at(latest["req"])
            if node._current_frame == latest["req"] and node.is_ready:
                return time.perf_counter() - t0
            time.sleep(0.001)
        return -1.0

    latest = {"req": 0}

    # ── 模式 1：frame-by-frame 逐帧步进（暂停态 ←/→ 键）──
    eval_at(200)
    drain_until(200, 5.0)
    settles = []
    for i in range(201, 221):
        latest["req"] = i
        eval_at(i)
        t = drain_until(i, 3.0)
        settles.append(t * 1000)
    ok = [s for s in settles if s >= 0]
    results["step"] = (len(ok), float(np.median(ok)) if ok else -1,
                       max(ok) if ok else -1)

    # ── 模式 2：随机来回拖拽（±1..30 帧，30ms 一个事件）──
    import random
    random.seed(42)
    pos = 300
    events = []
    _last = {"f": node._current_frame}
    t_end = time.perf_counter() + 5.0
    t_next = time.perf_counter()
    shown = []
    t_start = time.perf_counter()
    while time.perf_counter() < t_end:
        now = time.perf_counter()
        if now >= t_next:
            pos = max(0, min(total - 1, pos + random.randint(-30, 30)))
            latest["req"] = pos
            events.append(pos)
            eval_at(pos)
            t_next = now + 0.03
        if notify.pending:
            notify.pending = False
            eval_at(latest["req"])
        if node.is_ready and node._current_frame != _last.get("f", -999):
            _last["f"] = node._current_frame
            shown.append(node._current_frame)
        time.sleep(0.001)
    drag_shown = len(shown)
    results["drag"] = (drag_shown / 5.0,)  # 拖拽期间每秒上屏帧数

    # 结尾定格
    latest["req"] = pos
    eval_at(pos)
    t_settle = drain_until(pos, 5.0)
    results["drag_settle"] = (t_settle * 1000 if t_settle >= 0 else -1,
                              node._current_frame == pos)

    # ── 模式 3：快速单向连跳（+6 帧/25ms，像快速拖过多帧）──
    pos = 400
    eval_at(pos); drain_until(pos, 5.0)
    t_end = time.perf_counter() + 4.0
    t_next = time.perf_counter()
    shown = []
    _last = {"f": node._current_frame}
    while time.perf_counter() < t_end:
        now = time.perf_counter()
        if now >= t_next:
            pos = min(total - 1, pos + 6)
            latest["req"] = pos
            eval_at(pos)
            t_next = now + 0.025
        if notify.pending:
            notify.pending = False
            eval_at(latest["req"])
        if node.is_ready and node._current_frame != _last.get("f", -999):
            _last["f"] = node._current_frame
            shown.append(node._current_frame)
        time.sleep(0.001)
    results["fast"] = (len(shown) / 4.0,)
    latest["req"] = pos
    eval_at(pos)
    t_settle = drain_until(pos, 5.0)
    results["fast_settle"] = (t_settle * 1000 if t_settle >= 0 else -1,
                              node._current_frame == pos)

    print(f"=== {name} ===")
    n, med, mx = results["step"]
    print(f"  逐帧步进 20 次: 成功 {n}/20, settle 中位 {med:.0f}ms 最大 {mx:.0f}ms")
    print(f"  随机拖拽 5s: 上屏 {results['drag'][0]:.1f} 帧/秒（跟手度）")
    s, ok = results["drag_settle"]
    print(f"  拖拽结束定格: settle {s:.0f}ms 正确={ok}")
    print(f"  快速连跳 4s: 上屏 {results['fast'][0]:.1f} 帧/秒")
    s, ok = results["fast_settle"]
    print(f"  连跳结束定格: settle {s:.0f}ms 正确={ok}")

    node.cleanup()
    gl.release()


if __name__ == "__main__":
    root = os.environ.get("TC_BENCH_MEDIA_ROOT",
                        os.path.join(_REPO_ROOT, "tests", "test_videos"))
    default = ["4K_prores422HQ.mov", "4K_H265.mov", "2K_H265.mov", "4K_H264.mov"]
    paths = sys.argv[1:] or [os.path.join(root, d) for d in default]
    for p in paths:
        run_scrub(p, os.path.basename(p))
