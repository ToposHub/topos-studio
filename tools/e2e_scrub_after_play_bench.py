#!/usr/bin/env python
"""Diagnose "scrub becomes laggy after playing once".

Phases on the SAME renderer/timeline (single clip, mirrors user report):
  A. fresh fast scrub  -> shown fps + decode stats
  B. playback 8s       -> engine realtime path
  C. fast scrub again  -> compare against A

Per phase we collect:
  - scrub shown fps / stall count
  - decode source_type distribution (vt vs cpu fallback)
  - decode elapsed_ms p50/p90 from trace collector
  - open decoder session count in EditGpuDecodeManager

Run: .venv/bin/python tools/e2e_scrub_after_play_bench.py [video_name]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("TOPOS_EDIT_PLAYBACK_TRACE", "summary")

import av
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit import facade as edit_facade
from src.shared.media.metadata import Rational
from src.shared.video.playback_frame_trace import get_trace_collector, reset_trace_collector

VIDEOS = {
    "4K_H264": "tests/test_videos/4K_H264.mov",
    "4K_H265": "tests/test_videos/4K_H265.mov",
    "2K_prores422HQ": "tests/test_videos/2K_prores422HQ.mov",
    "4K_prores422HQ": "tests/test_videos/4K_prores422HQ.mov",
}


def build_sequence(path: str, n_cuts: int = 1, n_tracks: int = 1):
    c = av.open(path)
    s = c.streams.video[0]
    width, height = s.codec_context.width, s.codec_context.height
    num, den = s.average_rate.numerator, s.average_rate.denominator
    total = s.frames or 700
    fps = num / den if den else 25.0
    c.close()
    metadata = {"width": width, "height": height, "fps": fps,
                "total_frames": total, "duration_seconds": total / fps,
                "codec_name": "auto"}
    project = EditProject.create("Bench").import_asset(
        path=os.path.abspath(path), media_type="video", metadata=metadata)
    asset_id = next(iter(project.assets))

    # n_cuts>1 时模拟真实剪辑：同一素材切成多个 clip（各持独立
    # decoder_stream_key → 各自的 VT 会话 + 交互池克隆）
    if n_cuts <= 1:
        clips = [ClipInstance(clip_id="clip-001", asset_id=asset_id, track_id="track-001",
                              timeline_start=0, timeline_duration=total, source_start=0,
                              source_duration=total, speed=1.0)]
    else:
        cut_len = max(24, (total - 2 * n_cuts) // n_cuts)
        clips = []
        for i in range(n_cuts):
            start = i * cut_len
            if start + cut_len > total:
                break
            clips.append(ClipInstance(
                clip_id=f"cut-{i:03d}", asset_id=asset_id, track_id="track-001",
                timeline_start=start, timeline_duration=cut_len,
                source_start=start, source_duration=cut_len, speed=1.0,
            ))
    track = EditTrack(track_id="track-001", name="V1", track_type="video",
                      index=0, clips=tuple(clips))
    tracks = [track]
    seq_total = sum(cl.timeline_duration for cl in clips)
    # P3.4 多轨审计：把同一素材整段叠到 N 条轨道（不同 source 偏移 →
    # 各自独立解码流），每帧需要对 N 层并发解码。
    if n_tracks > 1:
        for t in range(1, n_tracks):
            offset = (total // 4) * (t % 3)  # 不同轨道取不同源段，避免纯重复
            layered = ClipInstance(
                clip_id=f"layer-{t:02d}", asset_id=asset_id, track_id=f"track-{t + 1:03d}",
                timeline_start=0, timeline_duration=seq_total,
                source_start=offset, source_duration=seq_total, speed=1.0,
                # opacity<1 豁免遮挡剪枝（_is_full_frame_opaque_video_layer），
                # 保证每帧真的对 N 层并发解码。
                opacity=0.9,
            )
            tracks.append(EditTrack(
                track_id=f"track-{t + 1:03d}", name=f"V{t + 1}", track_type="video",
                index=t, clips=(layered,),
            ))
    seq = TimelineSequence(timeline_id="seq-bench", name="Bench", fps=Rational(25, 1),
                           width=width, height=height, video_tracks=tuple(tracks))
    return project, seq, seq_total, fps


def pct(values, p):
    if not values:
        return None
    vs = sorted(values)
    return vs[min(len(vs) - 1, int(round(p / 100.0 * (len(vs) - 1))))]


def dm_session_stats(dm) -> dict:
    """Count open decoder sessions + reorder buffer fill held by the manager."""
    sources = getattr(dm, "_sources", {}) or {}
    cache = getattr(dm, "_source_cache", {}) or {}
    pool = getattr(dm, "_interactive_source_pool", {}) or {}
    pool_total = sum(len(v) for v in pool.values())
    ids = set()
    reorder = 0
    for s in list(sources.values()) + [t[0] for t in cache.values()]:
        if s is not None:
            ids.add(id(s))
            buf = getattr(s, "_reorder_buffer", None)
            if isinstance(buf, dict):
                reorder += len(buf)
    for v in pool.values():
        for s, _t in v:
            if s is not None:
                ids.add(id(s))
                buf = getattr(s, "_reorder_buffer", None)
                if isinstance(buf, dict):
                    reorder += len(buf)
    return {
        "sources": len(sources),
        "source_cache": len(cache),
        "pool_clones": pool_total,
        "unique_sessions": len(ids),
        "reorder_buf": reorder,
        "cursors": _cursor_layout(dm),
    }


def _cursor_layout(dm) -> str:
    """主源 + 池克隆的当前游标帧号与 busy 占用（P=主源, C=克隆）。"""
    parts = []
    primary = getattr(dm, "_sources", {}) or {}
    for s in primary.values():
        if s is not None:
            parts.append(f"P{getattr(s, 'current_frame', '?')}")
    cache = getattr(dm, "_source_cache", {}) or {}
    for t in cache.values():
        s = t[0] if isinstance(t, tuple) else t
        if s is not None:
            parts.append(f"K{getattr(s, 'current_frame', '?')}")
    busy = getattr(dm, "_interactive_busy_sources", set()) or set()
    for v in (getattr(dm, "_interactive_source_pool", {}) or {}).values():
        for s, _t in v:
            if s is not None:
                mark = "*" if id(s) in busy else ""
                parts.append(f"C{getattr(s, 'current_frame', '?')}{mark}")
    return ",".join(parts)


def phase_report(label, shown, ready_times, traces, dm) -> None:
    # 按到达顺序分早/中/晚三段看 decode_ms 分布（区分冷启动瞬态 vs 全程劣化）
    decodes = [(int(getattr(t, "frame_index", -1)), float(getattr(t, "decode_ms", 0.0) or 0.0))
               for t in traces]
    decode_ms = [ms for _f, ms in decodes]
    third = max(1, len(decodes) // 3)
    segs = {
        "early": [ms for _f, ms in decodes[:third]],
        "mid": [ms for _f, ms in decodes[third:2 * third]],
        "late": [ms for _f, ms in decodes[2 * third:]],
    }
    paths: dict[str, int] = {}
    for t in traces:
        k = str(getattr(t, "decode_path", ""))
        paths[k] = paths.get(k, 0) + 1
    fps = 0.0
    if len(ready_times) >= 2:
        fps = (len(ready_times) - 1) / (ready_times[-1][0] - ready_times[0][0] + 1e-9)
    stalls = 0
    if ready_times:
        last = ready_times[0][0]
        for t, _f in ready_times[1:]:
            if t - last > 0.3:
                stalls += 1
            last = t
    seg_txt = " ".join(
        f"{name}:p90={pct(v, 90):.0f}ms" if v else f"{name}:n/a"
        for name, v in segs.items()
    )
    print(
        f"  [{label}] shown={shown:4d} ({fps:5.1f}fps) stalls>300ms={stalls} | "
        f"decode p50={pct(decode_ms, 50)}ms p90={pct(decode_ms, 90)}ms [{seg_txt}] | "
        f"paths={paths} | sessions={dm_session_stats(dm)}"
    )
    slowest = sorted(traces, key=lambda t: -float(getattr(t, "decode_ms", 0.0) or 0.0))[:6]
    for t in slowest:
        try:
            d = t.to_compact_dict()
        except Exception:
            d = {"frame": getattr(t, "frame_index", "?"), "decode_ms": getattr(t, "decode_ms", "?")}
        print(f"        slow: {d}")


def run_heal_probe(app, renderer, controller, total, dm, mode: str) -> None:
    """判别实验：哪种操作能"治愈"播放后的拖拽劣化。

    paused_last: 暂停态精确渲染最后一帧（游标→末尾 + EOF 冲洗，无播放态切换）
    paused_mid : 暂停态精确渲染中间帧（游标→中部，无播放态切换）
    play_last  : 播放态单次渲染最后一帧（≈ 用户空格操作）
    play_mid   : 播放态单次渲染中间帧
    """
    target = total - 2 if mode.endswith("last") else max(2, total // 2)
    if mode.startswith("play"):
        renderer.notify_playback_state(True)
        controller.state.value = "playing"
        controller.current_frame = target
    renderer.set_playhead(target)  # 内部触发 request_render(target)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 4.0:
        app.processEvents()
        time.sleep(0.005)
        if renderer._cache.get(target) is not None:
            break
    if mode.startswith("play"):
        renderer.notify_playback_state(False)
        controller.state.value = "paused"
    time.sleep(0.4)
    app.processEvents()
    if mode == "paused_mid_realign":
        # 判别：系统安静时显式补一次池重对齐（播放刚停止时 realign 会被
        # 在飞渲染争抢打空——这里验证"安静时真正执行"是否就是解药）
        renderer._realign_interactive_pool_after_playback()
        time.sleep(0.8)
        app.processEvents()
    print(f"  [heal-{mode}] frame={target} sessions={dm_session_stats(dm)}")


def run_blank_playback_phase(app, renderer, controller, total, fps, duration_s=1.5,
                             bisect: str = "full"):
    """用户实验：播放头放到时间线末尾（无内容区）按播放再停止。

    无解码发生，只有播放状态机空转一圈（notify_playback_state True→False
    + engine miss/hold + tick 循环）。若这一圈能恢复拖拽速度，说明劣化
    来自播放状态残留而非解码游标。

    bisect:
      "notify" 仅 notify_playback_state(True)→(False) 开关周期（无渲染请求）
      "render" 状态周期 + request_render tick（经 engine miss 路径）
      "full"   状态周期 + set_playhead + request_render tick
    """
    reset_trace_collector()
    if bisect == "notify":
        renderer.notify_playback_state(True)
        controller.state.value = "playing"
        end_at = time.perf_counter() + 0.3
        while time.perf_counter() < end_at:
            app.processEvents()
            time.sleep(0.01)
        renderer.notify_playback_state(False)
        controller.state.value = "paused"
        time.sleep(0.3)
        app.processEvents()
        print("  [bisect-notify] done")
        return

    renderer.notify_playback_state(True)
    controller.state.value = "playing"
    if bisect == "full":
        controller.current_frame = total - 1
        renderer.set_playhead(total - 1)
    state = {"t0": time.perf_counter()}

    def tick():
        if time.perf_counter() - state["t0"] >= duration_s:
            timer.stop()
            return
        renderer.request_render(total - 1)

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(40)
    end_at = time.perf_counter() + duration_s + 0.5
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)
    timer.stop()
    renderer.notify_playback_state(False)
    controller.state.value = "paused"
    time.sleep(0.3)
    app.processEvents()
    print(f"  [bisect-{bisect}] done")


def run_scrub_phase(app, renderer, controller, total, dm, label, duration_s=4.0,
                    start_at: int | None = None):
    reset_trace_collector()
    ready_times = []
    count = {"n": 0}
    call_ms: list[float] = []  # request_scrub_preview 主线程调用耗时
    loop_lags: list[float] = []  # 事件循环滞后（timer 间隔 vs 实际）

    def on_ready(frame, result):
        count["n"] += 1
        ready_times.append((time.perf_counter(), int(frame)))
        if isinstance(result, dict) and result.get("ack_cb"):
            ack = result["ack_cb"]
            slot = getattr(result.get("present_frame"), "slot_id", -1)
            QTimer.singleShot(0, lambda: ack(slot))

    renderer.frame_ready.connect(on_ready)
    start_frame = 100 if start_at is None else max(100, min(start_at, total - 12))
    renderer.begin_scrub(start_frame)
    lo, hi = start_frame, total - 10
    state = {"frame": start_frame, "dir": 1, "next_at": time.perf_counter(),
             "t0": time.perf_counter(), "last_step": time.perf_counter()}

    def step():
        now = time.perf_counter()
        if now - state["t0"] >= duration_s:
            timer.stop()
            return
        loop_lags.append((now - state["last_step"]) * 1000.0)
        state["last_step"] = now
        if now >= state["next_at"]:
            state["frame"] += 3 * state["dir"]
            if state["frame"] >= hi:
                state["frame"] = hi
                state["dir"] = -1
            elif state["frame"] <= lo:
                state["frame"] = lo
                state["dir"] = 1
            # 生产中播放头移动会同步 controller.current_frame；不同步会被
            # _is_playback_result_stale 当过期帧丢弃（复现脚本曾因此失真）
            controller.current_frame = state["frame"]
            t_call = time.perf_counter()
            renderer.request_scrub_preview(state["frame"])
            call_ms.append((time.perf_counter() - t_call) * 1000.0)
            state["next_at"] = now + 0.008

    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(2)
    end_at = time.perf_counter() + duration_s + 0.8
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)
    renderer.finish_scrub(state["frame"])
    end_at = time.perf_counter() + 0.6
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)
    renderer.frame_ready.disconnect(on_ready)
    print(
        f"        ui: request_call p50={pct(call_ms, 50):.2f}ms "
        f"p90={pct(call_ms, 90):.2f}ms max={max(call_ms) if call_ms else 0:.1f}ms | "
        f"loop_lag p50={pct(loop_lags, 50):.1f}ms p90={pct(loop_lags, 90):.1f}ms"
    )
    phase_report(label, count["n"], ready_times, get_trace_collector().get_traces(), dm)


def run_playback_phase(app, renderer, controller, total, fps, duration_s=8.0):
    reset_trace_collector()
    ready_times = []
    count = {"n": 0}

    def on_ready(frame, result):
        count["n"] += 1
        ready_times.append((time.perf_counter(), int(frame)))
        if isinstance(result, dict) and result.get("ack_cb"):
            ack = result["ack_cb"]
            slot = getattr(result.get("present_frame"), "slot_id", -1)
            QTimer.singleShot(0, lambda: ack(slot))

    renderer.frame_ready.connect(on_ready)
    renderer.notify_playback_state(True)
    controller.state.value = "playing"
    interval_ms = max(16, min(100, int(1000 / fps)))
    state = {"base_frame": 100, "base_t": time.perf_counter(), "t0": time.perf_counter(),
             "last_req": None, "last_emitted": 100}

    def tick():
        now = time.perf_counter()
        if now - state["t0"] >= duration_s:
            timer.stop()
            return
        target = state["base_frame"] + int((now - state["base_t"]) * fps)
        if target > total - 5:
            target = total - 5
        controller.current_frame = target
        if renderer.should_defer_playback_tick(target):
            state["base_frame"] = state["last_emitted"] + 1
            state["base_t"] = now
            return
        if target != state["last_req"]:
            state["last_req"] = target
            # 生产中面板会同步播放头；realign/缓存依赖 _playhead_frame 正确
            renderer.set_playhead(target)
            renderer.request_render(target)

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(interval_ms)
    end_at = time.perf_counter() + duration_s + 1.2
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)
    timer.stop()
    renderer.notify_playback_state(False)
    controller.state.value = "paused"
    renderer.frame_ready.disconnect(on_ready)
    fps_shown = 0.0
    if len(ready_times) >= 2:
        fps_shown = (len(ready_times) - 1) / (ready_times[-1][0] - ready_times[0][0] + 1e-9)
    print(f"  [playback] shown={count['n']} ({fps_shown:.1f}fps)")
    time.sleep(0.3)
    app.processEvents()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", default="4K_H264", choices=list(VIDEOS))
    ap.add_argument("--cuts", type=int, default=8,
                    help="number of cuts of the same asset on the timeline")
    ap.add_argument("--tracks", type=int, default=1,
                    help="P3.4 multi-track audit: stack N full-length layers on N tracks")
    ap.add_argument("--play-s", type=float, default=10.0)
    ap.add_argument("--scrub-from-playhead", action="store_true",
                    help="C/D 阶段从播放停止帧开始拖拽（真实用户行为）")
    ap.add_argument("--idle-instead-of-blankplay", action="store_true",
                    help="对照实验：用等长纯等待替换空白播放（区分'状态重置'与'时间自愈'）")
    ap.add_argument("--bisect", default="full", choices=["notify", "render", "full"],
                    help="空白播放二分：notify=仅状态周期；render=状态+渲染；full=完整")
    ap.add_argument("--heal-probe", default=None,
                    choices=["paused_last", "paused_mid", "play_last", "play_mid",
                             "paused_mid_realign"],
                    help="判别实验：播放后用指定操作替代空白播放，再测第一次拖拽")
    args = ap.parse_args()
    path = os.path.join(ROOT, VIDEOS[args.video])
    print(f"== scrub-after-play bench: {args.video} cuts={args.cuts} tracks={args.tracks} ==")

    app = QGuiApplication.instance() or QGuiApplication([])
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    surface = QOffscreenSurface()
    surface.create()
    ctx = QOpenGLContext()
    if not ctx.create():
        print("FAILED to create GL context")
        return 1
    ctx.makeCurrent(surface)

    project, seq, total, fps = build_sequence(path, n_cuts=args.cuts, n_tracks=args.tracks)
    provider = Mock()
    provider.request_frame.return_value = None
    provider.open_clip.return_value = "clip-001"

    renderer = TimelinePreviewRenderer(
        project=project, sequence=seq, frame_provider=provider,
        main_context=ctx, viewport_size=(1920, 1080))
    dm = edit_facade.create_edit_gpu_decode_manager()
    gr = edit_facade.create_edit_gpu_preview_renderer(main_context=ctx)
    gr.start()
    renderer.set_gpu_renderer(gpu_decode_manager=dm, gpu_renderer=gr)
    renderer.set_comp_gl_context(ctx)

    controller = SimpleNamespace(
        state=SimpleNamespace(value="paused"),
        playback_direction="forward",
        current_frame=100,
    )
    renderer.set_playback_controller(controller)

    # warm one exact frame
    renderer.request_render(50)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5.0:
        app.processEvents()
        time.sleep(0.005)
        if renderer._cache.get(50) is not None:
            break

    run_scrub_phase(app, renderer, controller, total, dm, "A fresh-scrub   ")
    run_playback_phase(app, renderer, controller, total, fps, duration_s=args.play_s)
    c_start = int(controller.current_frame) if args.scrub_from_playhead else None
    # 用户操作顺序：播放后【先】空白播放/等待，【再】第一次拖拽。
    # --idle-instead-of-blankplay：对照——等长纯等待，区分"状态重置"与"时间自愈"
    if args.idle_instead_of_blankplay:
        t_wait = time.perf_counter()
        while time.perf_counter() - t_wait < 1.8:
            app.processEvents()
            time.sleep(0.01)
        print("  [idle-wait ] done (1.8s pure wait, control)")
    elif args.heal_probe:
        run_heal_probe(app, renderer, controller, total, dm, args.heal_probe)
    else:
        run_blank_playback_phase(app, renderer, controller, total, fps,
                                 bisect=args.bisect)
    run_scrub_phase(app, renderer, controller, total, dm, "C after-wait   ",
                    start_at=c_start)
    # 第二次拖拽（无论恢复机制是什么，这轮应是快的）
    run_scrub_phase(app, renderer, controller, total, dm, "D second-scrub  ",
                    start_at=c_start)

    renderer.shutdown()
    gr.stop()
    gr.wait(3000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
