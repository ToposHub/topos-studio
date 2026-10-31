#!/usr/bin/env python
"""End-to-end PLAYBACK benchmark: real TimelinePreviewRenderer + RealtimePlaybackEngine
+ EditGpuDecodeManager + EditGpuPreviewRenderer (GL) + real 4K media.

Mirrors the production playback chain:
  PlaybackControllerTimer tick -> should_defer_playback_tick -> request_render(frame)
  -> request_present_via_engine -> SourceResolver -> worker decode (parallel for
  multi-track) -> GPU composite -> frame_ready -> (ProgramViewer present + ack)

Run:
  .venv/bin/python tools/e2e_playback_bench.py            # 全部场景
  .venv/bin/python tools/e2e_playback_bench.py --only 4K_H264 dual4k
  .venv/bin/python tools/e2e_playback_bench.py --duration 10
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
    "2K_h264": "tests/test_videos/2K_h264.mp4",
    "2K_H265": "tests/test_videos/2K_H265.mov",
    "2K_prores422HQ": "tests/test_videos/2K_prores422HQ.mov",
    "2K_DNxHR": "tests/test_videos/2K_DNxHR HQX 12-bit.mov",
    "4K_H264": "tests/test_videos/4K_H264.mov",
    "4K_H265": "tests/test_videos/4K_H265.mov",
    "4K_prores422HQ": "tests/test_videos/4K_prores422HQ.mov",
    "4K_prores4444": "tests/test_videos/4K_prores4444.mov",
    "4K_DNxHR": "tests/test_videos/4K_DNxHR HQX 12-bit.mov",
}

SCENARIOS: dict[str, list[tuple[str, float]]] = {
    # 单轨：每条素材一个场景
    **{name: [(name, 1.0)] for name in VIDEOS},
    # 多轨：不同素材叠轨，上层半透明（触发混合合成 + 并行多路解码）
    "dual2k": [("2K_h264", 1.0), ("2K_H265", 0.5)],
    "dual4k": [("4K_H264", 1.0), ("4K_H265", 0.5)],
    "dual4k_prores": [("4K_prores422HQ", 1.0), ("4K_prores4444", 0.5)],
    "dual4k_dn": [("4K_H264", 1.0), ("4K_DNxHR", 0.5)],
    "quad4k": [
        ("4K_H264", 1.0),
        ("4K_H265", 0.5),
        ("4K_prores422HQ", 0.35),
        ("4K_DNxHR", 0.35),
    ],
}


def probe(path: str) -> tuple[int, int, float, int]:
    c = av.open(path)
    s = c.streams.video[0]
    width, height = s.codec_context.width, s.codec_context.height
    num, den = s.average_rate.numerator, s.average_rate.denominator
    total = s.frames or 700
    c.close()
    return width, height, (num / den if den else 25.0), total


class _BenchController:
    """Playback controller stub mirroring the fields the renderer reads."""

    def __init__(self, frame: int) -> None:
        self.state = SimpleNamespace(value="playing")
        self.playback_direction = "forward"
        self.current_frame = int(frame)


def build_sequence(entries: list[tuple[str, float]]):
    metas = []
    project = EditProject.create("Bench")
    for name, _opacity in entries:
        path = os.path.join(ROOT, VIDEOS[name])
        width, height, fps, total = probe(path)
        metas.append((path, width, height, fps, total))
        project = project.import_asset(
            path=path,
            media_type="video",
            metadata={
                "width": width, "height": height, "fps": fps,
                "total_frames": total, "duration_seconds": total / fps,
                "codec_name": "auto",
            },
        )
    asset_ids = list(project.assets)
    total = min(m[4] for m in metas)
    fps = metas[0][3]
    seq_w, seq_h = metas[0][1], metas[0][2]
    tracks = []
    for i, (_path, _w, _h, _fps, _tot) in enumerate(metas):
        opacity = entries[i][1]
        clip = ClipInstance(
            clip_id=f"clip-{i:03d}", asset_id=asset_ids[i], track_id=f"track-{i:03d}",
            timeline_start=0, timeline_duration=total, source_start=0,
            source_duration=total, speed=1.0, opacity=opacity,
        )
        tracks.append(EditTrack(
            track_id=f"track-{i:03d}", name=f"V{i + 1}", track_type="video",
            index=i, clips=(clip,),
        ))
    seq = TimelineSequence(
        timeline_id="seq-bench", name="Bench", fps=Rational(int(round(fps * 1000)), 1000),
        width=seq_w, height=seq_h, video_tracks=tuple(tracks),
    )
    return project, seq, total, fps


def run_scenario(app, ctx, entries, duration_s=6.0, start_frame=100) -> dict:
    project, seq, total, fps = build_sequence(entries)
    provider = Mock()
    provider.request_frame.return_value = None
    provider.open_clip.return_value = "clip-000"

    renderer = TimelinePreviewRenderer(
        project=project, sequence=seq, frame_provider=provider,
        main_context=ctx, viewport_size=(1920, 1080),
    )
    dm = edit_facade.create_edit_gpu_decode_manager()
    gr = edit_facade.create_edit_gpu_preview_renderer(main_context=ctx)
    gr.render_error.connect(lambda f, m: print(f"    [gpu_error] {f}: {m}"))
    gr.start()
    renderer.set_gpu_renderer(gpu_decode_manager=dm, gpu_renderer=gr)
    renderer.set_comp_gl_context(ctx)

    # 实验开关：多轨解码并行度（默认 2）
    workers = int(os.environ.get("TOPOS_BENCH_DECODE_WORKERS") or 0)
    if workers > 0:
        worker = getattr(renderer, "_worker", None)
        old_exec = getattr(worker, "_decode_executor", None)
        if worker is not None and old_exec is not None:
            from concurrent.futures import ThreadPoolExecutor
            worker._decode_executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="EditPreviewDecodeBench",
            )
            old_exec.shutdown(wait=False)

    # ---- paused warm render (seek path), mirrors pressing play from a still ----
    controller = _BenchController(start_frame)
    renderer.set_playback_controller(controller)
    renderer.request_render(start_frame)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 8.0:
        app.processEvents()
        time.sleep(0.005)
        if renderer._cache.get(start_frame) is not None:
            break

    reset_trace_collector()
    stats = {
        "requests": 0, "deferred": 0, "emitted": 0, "emit_times": [],
        "request_at": {}, "latencies": [], "start": time.perf_counter(),
        "last_emitted": start_frame, "last_req": None,
        "ticks": 0, "tick_ivs": [], "last_tick_t": None, "rr_durs": [],
        "tick_marks": [], "req_marks": [],
    }

    def on_ready(frame, result):
        now = time.perf_counter()
        stats["emitted"] += 1
        stats["emit_times"].append((now - stats["start"], int(frame)))
        stats["last_emitted"] = int(frame)
        req_t = stats["request_at"].pop(int(frame), None)
        if req_t is not None:
            stats["latencies"].append((now - req_t) * 1000.0)
        if isinstance(result, dict) and result.get("ack_cb"):
            ack = result["ack_cb"]
            slot = getattr(result.get("present_frame"), "slot_id", -1)
            QTimer.singleShot(0, lambda: ack(slot))

    renderer.frame_ready.connect(on_ready)
    renderer.notify_playback_state(True)  # -> engine REALTIME + prewarm

    interval_ms = max(16, min(100, int(1000 / fps)))
    state = {"base_frame": start_frame, "base_t": time.perf_counter(), "t0": time.perf_counter()}

    def tick():
        now = time.perf_counter()
        if now - state["t0"] >= duration_s:
            timer.stop()
            return
        stats["ticks"] += 1
        if stats["last_tick_t"] is not None:
            stats["tick_ivs"].append((now - stats["last_tick_t"]) * 1000.0)
        stats["last_tick_t"] = now
        target = state["base_frame"] + int((now - state["base_t"]) * fps)
        if target > total - 5:
            target = total - 5
        stats["tick_marks"].append((now - stats["start"], target, state["base_frame"]))
        controller.current_frame = target
        # TimelinePanel 背压：defer 时 re-anchor（时钟回到当前可见帧）
        # （TOPOS_BENCH_NO_DEFER=1 时跳过，用于隔离背压策略本身的影响）
        if not os.environ.get("TOPOS_BENCH_NO_DEFER"):
            if renderer.should_defer_playback_tick(target):
                state["base_frame"] = stats["last_emitted"] + 1
                state["base_t"] = now
                stats["deferred"] += 1
                return
        if target != stats["last_req"]:
            stats["requests"] += 1
            stats["req_marks"].append((now - stats["start"], target))
            stats["request_at"][target] = now
            stats["last_req"] = target
            t_rr = time.perf_counter()
            renderer.request_render(target)
            stats["rr_durs"].append((time.perf_counter() - t_rr) * 1000.0)

    timer = QTimer()
    # 与生产 PlaybackControllerTimer 一致用 PreciseTimer：默认 CoarseTimer 在
    # Windows 上 40ms 会被系统时钟取整到 ~47ms，把帧率硬压到 ~21Hz
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(interval_ms)

    end_at = time.perf_counter() + duration_s + 1.2
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)

    renderer.notify_playback_state(False)
    renderer.frame_ready.disconnect(on_ready)
    time.sleep(0.05)
    app.processEvents()

    # ---- per-second histogram for diagnosis ----
    emits = stats["emit_times"]
    dur_i = int(duration_s) + 1
    sec_ticks = [0] * dur_i
    sec_reqs = [0] * dur_i
    sec_emits = [0] * dur_i
    hist = {
        "ticks": sec_ticks, "reqs": sec_reqs, "emits": sec_emits,
    }
    for mark in stats.get("tick_marks", []):
        t = mark[0]
        if 0 <= int(t) < dur_i:
            sec_ticks[int(t)] += 1
    for t, _f in stats.get("req_marks", []):
        if 0 <= int(t) < dur_i:
            sec_reqs[int(t)] += 1
    for t, _f in emits:
        if 0 <= int(t) < dur_i:
            sec_emits[int(t)] += 1

    # ---- summary ----
    shown_fps = 0.0
    if len(emits) >= 2:
        shown_fps = (len(emits) - 1) / (emits[-1][0] - emits[0][0] + 1e-9)
    frame_iv = 1.0 / fps
    gaps = [b[0] - a[0] for a, b in zip(emits, emits[1:])]
    stutters = sum(1 for g in gaps if g > 3 * frame_iv)
    max_gap = max(gaps) if gaps else 0.0
    lats = sorted(stats["latencies"])
    p50 = lats[len(lats) // 2] if lats else 0.0
    p95 = lats[int(len(lats) * 0.95)] if lats else 0.0
    # 启动期 vs 稳态：前 2s 的发射速率最能反映 warmup 风暴损失
    first_emit_ms = emits[0][0] * 1000.0 if emits else -1.0
    early = [t for t, _ in emits if t <= 2.0]
    late = [t for t, _ in emits if t > 2.0]
    fps_early = len(early) / 2.0
    if late:
        span = duration_s - 2.0
        fps_late = (len(late) - (1 if early else 0)) / max(span, 1e-9)
    else:
        fps_late = 0.0
    trace = get_trace_collector().get_summary()
    engine = getattr(renderer, "realtime_engine", None)
    eng_stats = {}
    if engine is not None and hasattr(engine, "stats"):
        try:
            eng_stats = engine.stats()
        except Exception:
            eng_stats = {}

    result = {
        "fps_target": fps,
        "shown_fps": shown_fps,
        "requests": stats["requests"],
        "deferred": stats["deferred"],
        "emitted": stats["emitted"],
        "stutters": stutters,
        "max_gap_ms": max_gap * 1000.0,
        "lat_p50_ms": p50,
        "lat_p95_ms": p95,
        "decode_avg_ms": trace.get("decode_avg_ms", 0.0),
        "render_avg_ms": trace.get("render_avg_ms", 0.0),
        "total_avg_ms": trace.get("total_avg_ms", 0.0),
        "cpu_fallback": trace.get("cpu_fallback_frames", 0),
        "decode_paths": trace.get("decode_path_counts", {}),
        "render_paths": trace.get("render_path_counts", {}),
        "fallback_reasons": trace.get("fallback_reasons", {}),
        "engine": eng_stats,
        "last_emitted": stats["last_emitted"],
        "last_req": stats["last_req"],
        "ticks": stats["ticks"],
        "tick_iv_avg_ms": (sum(stats["tick_ivs"]) / len(stats["tick_ivs"])) if stats["tick_ivs"] else 0.0,
        "tick_iv_max_ms": max(stats["tick_ivs"]) if stats["tick_ivs"] else 0.0,
        "rr_max_ms": max(stats["rr_durs"]) if stats["rr_durs"] else 0.0,
        "rr_avg_ms": (sum(stats["rr_durs"]) / len(stats["rr_durs"])) if stats["rr_durs"] else 0.0,
        "first_emit_ms": first_emit_ms,
        "fps_early": fps_early,
        "fps_late": fps_late,
        "hist": hist,
        "trace_full": trace,
        "tick_tail": [(round(m[0], 2), m[1], m[2]) for m in stats["tick_marks"][-24:]],
        "emit_tail": [(round(t, 2), f) for t, f in emits[-8:]],
    }

    renderer.shutdown()
    gr.stop()
    gr.wait(3000)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, help="scenario name filter")
    ap.add_argument("--duration", type=float, default=6.0)
    ap.add_argument("--start", type=int, default=100)
    args = ap.parse_args()

    # 实验开关：禁用交互池预热（隔离播放启动期 VT 会话风暴的影响）
    if os.environ.get("TOPOS_BENCH_NO_POOL_WARMUP"):
        from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
        EditGpuDecodeManager._ensure_interactive_source_pool_warmup = (
            lambda self, *a, **k: None
        )

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

    names = [n for n in SCENARIOS if not args.only or n in args.only]
    results = {}
    for name in names:
        entries = SCENARIOS[name]
        label = "+".join(e[0] for e in entries)
        print(f"\n== playback bench: {name} ({label}) ==")
        try:
            r = run_scenario(app, ctx, entries, duration_s=args.duration, start_frame=args.start)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  FAILED: {e}")
            continue
        results[name] = r
        print(
            f"  target={r['fps_target']:.0f}fps shown={r['shown_fps']:5.1f}fps "
            f"(0-2s:{r['fps_early']:5.1f} 2s+:{r['fps_late']:5.1f}) "
            f"req={r['requests']:3d} emit={r['emitted']:3d} defer={r['deferred']:3d} "
            f"stutter={r['stutters']:2d} maxgap={r['max_gap_ms']:5.0f}ms | "
            f"lat p50/p95={r['lat_p50_ms']:5.1f}/{r['lat_p95_ms']:5.1f}ms | "
            f"1st_emit={r['first_emit_ms']:5.0f}ms rr={r['rr_avg_ms']:.1f}/{r['rr_max_ms']:.0f}ms"
        )
        h = r.get("hist") or {}
        if h.get("ticks"):
            print("    per-sec ticks : " + " ".join(f"{n:3d}" for n in h["ticks"]))
            print("    per-sec reqs  : " + " ".join(f"{n:3d}" for n in h["reqs"]))
            print("    per-sec emits : " + " ".join(f"{n:3d}" for n in h["emits"]))
        if r["ticks"] - r["requests"] > 25:
            print(f"    tick_tail(t,target)={r['tick_tail']}")
            print(f"    emit_tail(t,frame)={r['emit_tail']}")
        if r["decode_paths"]:
            print(f"    decode_paths={r['decode_paths']}")
        tr = r.get("trace_full") or {}
        if tr:
            keys = ('gpu_frames','cpu_to_ndarray_frames','qimage_upload_frames',
                    'gl_tex_image_upload_frames','sync_seek_frames','texture_slot_reuse_without_ack')
            print("    trace_flags=" + str({k: tr.get(k) for k in keys}))
        if r.get("render_paths"):
            print(f"    render_paths={r['render_paths']}")
        if r["fallback_reasons"]:
            print(f"    fallback_reasons={r['fallback_reasons']}")
        eng = r.get("engine") or {}
        if eng:
            keep = {k: v for k, v in eng.items()
                    if any(s in k for s in ("presented", "miss", "request", "resolution", "hold", "rebuffer"))}
            if keep:
                print(f"    engine={keep}")

    print("\n==== summary ====")
    print(f"{'scenario':22s} {'target':>6s} {'shown':>6s} {'0-2s':>6s} {'2s+':>6s} {'emit':>5s} {'stut':>4s} {'lat_p50':>8s} {'lat_p95':>8s} {'1st_ms':>7s}")
    for name, r in results.items():
        print(
            f"{name:22s} {r['fps_target']:6.0f} {r['shown_fps']:6.1f} {r['fps_early']:6.1f} "
            f"{r['fps_late']:6.1f} {r['emitted']:5d} {r['stutters']:4d} "
            f"{r['lat_p50_ms']:8.1f} {r['lat_p95_ms']:8.1f} {r['first_emit_ms']:7.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
