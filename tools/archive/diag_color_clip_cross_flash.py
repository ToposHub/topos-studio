#!/usr/bin/env python3
"""
调色页「同一素材剪成多个片段」跨片段播放诊断 harness。

复刻 Color 页真实链路：
  EnhancedVideoPlaybackEngine（v3 双路径）
  + in/out range（片段源范围）
  + _on_play_tick 环绕（video_playback.py）
  + _maybe_advance_to_next_same_source_clip（file_controller.py）

记录：
  - 每次帧提交（frame_index / reason / quality / 时间戳）
  - 每次 tick 的 UI 线程耗时（检测阻塞 seek）
  - 片段边界处的显示序列：检测闪帧（范围外帧、回跳帧）与卡顿（提交间隔）

用法:
    .venv/bin/python tools/diag_color_clip_cross_flash.py [--file <video>] [--clips 3]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TOPOS_PLAYBACK_TRACE"] = "0"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import logging
logging.getLogger().setLevel(logging.ERROR)


def build_mock_image_engine():
    from unittest.mock import MagicMock
    engine = MagicMock()
    engine.renderer = MagicMock()
    engine.renderer.get_renderer_stats = MagicMock(return_value={
        'queue_depth': 0,
        'dropped_preview_frames': 0,
        'coalesced_video_commands': 0,
    })
    engine.viewport_size = (1920, 1080)
    engine.window = MagicMock()
    engine.window.config_manager = MagicMock()
    cfg = {
        'video_pipeline_mode': 'v3',
        'video_pipeline_allow_v2_fallback': False,
        'video_playback_resolution': 1920,
        'video_preview_max_queue': 3,
    }
    engine.window.config_manager.config = cfg
    engine.window.config_manager.get = lambda key, default=None: cfg.get(key, default)
    return engine


class ClipPlaybackSim:
    """模拟 file_controller 的片段上下文与跨片段推进逻辑。"""

    def __init__(self, controller, clips, preadvance=True):
        self.controller = controller
        self.clips = clips  # list of (source_start, source_end) inclusive
        self.preadvance = preadvance
        self.clip_idx = 0
        self.last_source_frame = None
        self.advancing = False
        self.events = []  # (t, kind, detail)
        self.finished = False
        self.clip_timeline = [(time.perf_counter(), 0)]  # 激活片段时间线
        controller.frame_changed.connect(self._on_source_frame_changed)

    def clip_idx_at(self, t: float) -> int:
        idx = 0
        for ts, ci in self.clip_timeline:
            if ts <= t:
                idx = ci
            else:
                break
        return idx

    def log(self, kind, detail):
        self.events.append((time.perf_counter(), kind, detail))

    def current_range(self):
        return self.clips[self.clip_idx]

    def apply_context(self):
        s, e = self.current_range()
        self.controller.set_in_out_range(s, e)

    def _on_source_frame_changed(self, source_frame: int):
        source_frame = int(source_frame)
        # 最后一个片段播完（环绕回 in 点）即结束，避免循环环绕污染统计
        s, e = self.current_range()
        if self.clip_idx >= len(self.clips) - 1 and source_frame == s and self.last_source_frame == e:
            try:
                self.controller.stop_playback()
            except Exception:
                pass
            self.finished = True
            return
        if self.preadvance:
            self._maybe_preadvance(source_frame)
        self._maybe_advance(source_frame)
        # 与 file_controller 一致：只记录落在当前片段源范围内的帧
        s, e = self.current_range()
        if s <= source_frame <= e:
            self.last_source_frame = source_frame

    def _maybe_preadvance(self, source_frame: int):
        """复刻 _maybe_preadvance_same_source_clip：到 out 帧时提前 rebind。"""
        if self.advancing:
            return
        if not bool(getattr(self.controller, "is_playing", False)):
            return
        s, e = self.current_range()
        if source_frame != e:
            return
        if self.clip_idx + 1 >= len(self.clips):
            return
        next_s = self.clips[self.clip_idx + 1][0]
        if next_s != e + 1:
            return  # 非连续源范围仍走 wrap + seek 路径
        self.advancing = True
        t0 = time.perf_counter()
        try:
            self.clip_idx += 1
            self.clip_timeline.append((time.perf_counter(), self.clip_idx))
            self.apply_context()
            self.log("preadvance.rebind", f"-> clip{self.clip_idx} {self.current_range()} (无seek)")
        finally:
            self.advancing = False
        self.log("preadvance.total_ms", f"{(time.perf_counter() - t0) * 1000.0:.1f}")

    def _maybe_advance(self, source_frame: int):
        """完全复刻 _maybe_advance_to_next_same_source_clip 的结构。"""
        if self.advancing:
            return
        s, e = self.current_range()
        last = self.last_source_frame
        duration = e - s + 1
        if last is None or source_frame != s:
            return
        # 真实环绕：上一帧必须在当前片段尾部（上界防止 rebind 后级联）
        if not (e - 1 <= last <= e + 1):
            return
        if duration <= 1:
            return
        if self.clip_idx + 1 >= len(self.clips):
            return
        self.advancing = True
        t0 = time.perf_counter()
        try:
            was_playing = bool(getattr(self.controller, "is_playing", False))
            self.clip_idx += 1
            self.clip_timeline.append((time.perf_counter(), self.clip_idx))
            self.apply_context()
            self.log("advance.rebind", f"-> clip{self.clip_idx} {self.current_range()}")
            seek_t0 = time.perf_counter()
            self.controller.seek_to_frame(int(self.clips[self.clip_idx][0]))
            seek_ms = (time.perf_counter() - seek_t0) * 1000.0
            self.log("advance.seek_done", f"target={self.clips[self.clip_idx][0]} took={seek_ms:.1f}ms")
            if was_playing:
                st_t0 = time.perf_counter()
                self.controller.start_playback()
                start_ms = (time.perf_counter() - st_t0) * 1000.0
                self.log("advance.start_done", f"took={start_ms:.1f}ms playing={self.controller.is_playing}")
        finally:
            self.advancing = False
        self.log("advance.total_ms", f"{(time.perf_counter() - t0) * 1000.0:.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "tests/test_videos/2K_h264.mp4"))
    ap.add_argument("--clip-frames", type=int, default=36, help="每个片段的源帧数")
    ap.add_argument("--clips", type=int, default=3)
    ap.add_argument("--reordered", action="store_true",
                    help="片段顺序打乱（模拟同一素材剪成乱序片段）")
    ap.add_argument("--legacy", action="store_true",
                    help="禁用 pre-advance，使用旧的 wrap+seek 推进逻辑做对比")
    ap.add_argument("--seconds", type=float, default=0.0, help="总播放时长上限（0=播完所有片段）")
    args = ap.parse_args()

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer
    app = QApplication.instance() or QApplication(sys.argv)

    from src.shared.video.video_playback import EnhancedVideoPlaybackEngine

    engine = build_mock_image_engine()
    controller = EnhancedVideoPlaybackEngine(engine)
    controller.enable_dual_path(True)

    print(f"[load] {args.file}")
    assert controller.load_video(args.file), "load_video failed"
    total = int(controller._range_total_frames() or 0)
    fps = float(getattr(controller._coordinator, "fps", 0) or 0) or 30.0
    print(f"[load] total_frames={total} fps={fps} dual_path={controller._use_dual_path}")

    # 构造片段：同一素材切成的连续片段，从第 10 帧开始避免 0 号帧干扰
    base = 10
    clips = []
    for i in range(args.clips):
        s = base + i * args.clip_frames
        e = min(total - 1, s + args.clip_frames - 1)
        if s >= total:
            break
        clips.append((s, e))
    print(f"[clips] {clips}")
    if args.reordered and len(clips) >= 3:
        # 乱序：第二段的源范围在第一段之前（in_frame 倒退）
        clips[0], clips[1] = clips[1], clips[0]
        print(f"[clips reordered] {clips}")

    sim = ClipPlaybackSim(controller, clips, preadvance=not args.legacy)

    # ── 记录 coordinator.tick() 返回的每一帧（含被丢弃的）──
    tick_frames = []  # (t, frame_index_or_None)
    _coord = controller._coordinator
    orig_coord_tick = _coord.tick

    def spy_coord_tick():
        f = orig_coord_tick()
        tick_frames.append((time.perf_counter(),
                            getattr(f, "frame_index", None) if f is not None else None))
        return f

    _coord.tick = spy_coord_tick

    # ── 记录每次提交到渲染器的帧 ──
    submissions = []  # (t, frame_num, reason, quality)
    orig_submit = controller._submit_decoded_frame_playback

    def spy_submit(frame, frame_num, *, quality="proxy", reason="playback", **kw):
        submissions.append((time.perf_counter(), int(frame_num), reason, quality))
        return orig_submit(frame, frame_num, quality=quality, reason=reason, **kw)

    controller._submit_decoded_frame_playback = spy_submit

    # ── 记录每次 tick 的耗时（检测 UI 线程阻塞）──
    tick_times = []
    orig_tick = controller._on_play_tick

    def spy_tick():
        t0 = time.perf_counter()
        orig_tick()
        tick_times.append(((time.perf_counter() - t0) * 1000.0, time.perf_counter()))

    controller._on_play_tick = spy_tick

    sim.apply_context()
    controller.seek_to_frame(clips[0][0])
    controller.start_playback()
    print(f"[play] start is_playing={controller.is_playing}")

    timer = QTimer()
    timer.setInterval(max(4, int(1000.0 / fps)))
    timer.timeout.connect(lambda: controller._on_play_tick())
    timer.start()

    last_clip_idx = [0]

    def check_done():
        # 播完最后一个片段后停止
        if sim.finished:
            app.quit()

    done_timer = QTimer()
    done_timer.setInterval(100)
    done_timer.timeout.connect(check_done)
    done_timer.start()

    t_start = time.perf_counter()
    timeout_s = args.seconds if args.seconds > 0 else (len(clips) * args.clip_frames / fps) + 6.0
    QTimer.singleShot(int(timeout_s * 1000), app.quit)
    app.exec()
    timer.stop()
    controller.stop_playback()
    wall = time.perf_counter() - t_start

    # ── 分析 ──
    print("\n" + "=" * 78)
    print("分析结果")
    print("=" * 78)

    t0 = submissions[0][0] if submissions else 0.0

    print("\n── 帧提交序列（围绕片段边界 ±6 条）──")
    boundaries = set()
    for t, kind, detail in sim.events:
        if kind in ("advance.rebind", "preadvance.rebind"):
            boundaries.add(t)
    for i, (t, fn, reason, q) in enumerate(submissions):
        near = any(abs(t - b) < 1.5 for b in boundaries) or i < 3
        if near:
            rng = None
            for ci, (s, e) in enumerate(clips):
                if s <= fn <= e:
                    rng = ci
                    break
            tag = "" if rng is not None else "  <<< 范围外帧!"
            print(f"  t={t - t0:7.3f}s frame={fn:5d} clip={rng} reason={reason:8s} q={q}{tag}")

    # 闪帧检测：提交的帧不属于提交时刻所属的片段
    print("\n── 闪帧/回跳检测 ──")
    flash = 0
    prev_fn = None
    for t, fn, reason, q in submissions:
        if prev_fn is not None and fn < prev_fn - 1 and reason == "playback":
            print(f"  回跳: t={t - t0:.3f}s {prev_fn} -> {fn} (reason={reason})")
            flash += 1
        prev_fn = fn
    # 每个提交帧归属片段 vs 时间线上当时激活的片段
    for t, fn, reason, q in submissions:
        owner = None
        for ci, (s, e) in enumerate(clips):
            if s <= fn <= e:
                owner = ci
                break
        active = sim.clip_idx_at(t)
        if owner is None:
            print(f"  范围外帧提交: t={t - t0:.3f}s frame={fn} reason={reason} active_clip={active}")
            flash += 1
        elif owner != active:
            print(f"  串扰帧(属于clip{owner}, 但当时激活clip{active}): "
                  f"t={t - t0:.3f}s frame={fn} reason={reason}")
            flash += 1

    # 卡顿检测：相邻提交间隔
    print("\n── 卡顿检测（相邻提交间隔 > 2 帧周期）──")
    budget = 2.0 / fps
    stalls = 0
    for i in range(1, len(submissions)):
        dt = submissions[i][0] - submissions[i - 1][0]
        if dt > budget:
            stalls += 1
            print(f"  t={submissions[i][0] - t0:7.3f}s gap={dt * 1000:7.1f}ms "
                  f"({submissions[i - 1][1]} -> {submissions[i][1]}, reason={submissions[i][2]})")

    # tick 阻塞检测
    print("\n── UI tick 阻塞（> 8ms）──")
    slow_ticks = [x for x in tick_times if x[0] > 8.0]
    for ms, t in slow_ticks:
        print(f"  t={t - t0:7.3f}s tick耗时={ms:7.1f}ms")

    # advance 事件
    print("\n── 跨片段推进事件 ──")
    for t, kind, detail in sim.events:
        print(f"  t={t - t0:7.3f}s {kind}: {detail}")

    # 边界附近 tick 返回的帧（含被丢弃帧）
    print("\n── 边界附近 coordinator.tick() 返回帧（±0.3s）──")
    for t, fn in tick_frames:
        if fn is None:
            continue
        near = any(abs(t - b) < 0.3 for b in boundaries)
        if not near:
            continue
        owner = None
        for ci, (s, e) in enumerate(clips):
            if s <= fn <= e:
                owner = ci
                break
        active = sim.clip_idx_at(t)
        tag = ""
        if owner != active:
            tag = f"  <<< 旧片段残留帧(属于clip{owner}, 激活clip{active})"
        print(f"  t={t - t0:7.3f}s tick帧={fn}{tag}")

    print(f"\n总计: 提交 {len(submissions)} 帧, 墙钟 {wall:.2f}s, "
          f"范围外/回跳帧 {flash}, 卡顿 {stalls}, 慢tick {len(slow_ticks)}")
    controller.unload_video()


if __name__ == "__main__":
    main()
