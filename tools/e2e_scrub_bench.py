#!/usr/bin/env python
"""End-to-end scrub benchmark: real TimelinePreviewRenderer + real
EditGpuDecodeManager + real EditGpuPreviewRenderer (GL) + real 4K media.

Simulates playhead drags at several speeds on the UI thread (QTimer-driven,
mirroring EditPage._emit_scrub_frame_request -> request_scrub_preview) and
counts how many frames actually reach frame_ready (i.e. get displayed).

Run:
  .venv/bin/python tools/e2e_scrub_bench.py [video_name] [--pattern slow|medium|fast|vfast]
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
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit import facade as edit_facade
from src.shared.media.metadata import Rational

VIDEOS = {
    "4K_H264": "tests/test_videos/4K_H264.mov",
    "4K_H265": "tests/test_videos/4K_H265.mov",
    "4K_prores422HQ": "tests/test_videos/4K_prores422HQ.mov",
    "4K_prores4444": "tests/test_videos/4K_prores4444.mov",
    "2K_prores422HQ": "tests/test_videos/2K_prores422HQ.mov",
    "4K_DNxHR": "tests/test_videos/4K_DNxHR HQX 12-bit.mov",
}

PATTERNS = {
    # label: (interval_ms, frames_per_step)
    "slow": (100.0, 1),     # 慢速一帧一帧拖
    "medium": (16.0, 1),    # 常速拖拽 (~60Hz 鼠标, 1f/事件)
    "fast": (8.0, 3),       # 稍快拖拽 (3f/事件)
    "vfast": (8.0, 10),     # 非常快 (甩动)
}


class Stats:
    def __init__(self):
        self.ready = 0
        self.ready_times = []      # (t, frame)
        self.requests = 0
        self.request_times = []    # (t, frame)
        self.started = time.perf_counter()

    def fps(self):
        if len(self.ready_times) < 2:
            return 0.0
        return (len(self.ready_times) - 1) / (self.ready_times[-1][0] - self.ready_times[0][0] + 1e-9)


def build_sequence(path: str):
    c = av.open(path)
    s = c.streams.video[0]
    width, height = s.codec_context.width, s.codec_context.height
    num, den = s.average_rate.numerator, s.average_rate.denominator
    total = s.frames or 700
    fps = num / den if den else 25.0
    c.close()
    metadata = {
        "width": width, "height": height, "fps": fps,
        "total_frames": total, "duration_seconds": total / fps,
        "codec_name": "auto",
    }
    project = EditProject.create("Bench").import_asset(
        path=os.path.abspath(path), media_type="video", metadata=metadata,
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-001", asset_id=asset_id, track_id="track-001",
        timeline_start=0, timeline_duration=total, source_start=0,
        source_duration=total, speed=1.0,
    )
    track = EditTrack(track_id="track-001", name="V1", track_type="video", index=0, clips=(clip,))
    seq = TimelineSequence(
        timeline_id="seq-bench", name="Bench", fps=Rational(25, 1),
        width=width, height=height, video_tracks=(track,),
    )
    return project, seq, total


def run_pattern(app, renderer, total, label, interval_ms, frames_per_step, duration_s=4.0):
    stats = Stats()

    def on_ready(frame, result):
        stats.ready += 1
        stats.ready_times.append((time.perf_counter() - stats.started, int(frame)))
        # mimic ProgramViewer paint ack: queue ack on UI thread like update()+paintGL
        if isinstance(result, dict) and "ack_cb" in result and result.get("ack_cb"):
            ack = result["ack_cb"]
            slot = getattr(result.get("present_frame"), "slot_id", -1)
            QTimer.singleShot(0, lambda: ack(slot))

    renderer.frame_ready.connect(on_ready)

    start_frame = 100
    renderer.begin_scrub(start_frame)
    lo, hi = 100, total - 10
    state = {"frame": start_frame, "dir": 1, "next_at": time.perf_counter(), "t0": time.perf_counter()}

    def step():
        now = time.perf_counter()
        if now - state["t0"] >= duration_s:
            timer.stop()
            return
        if now >= state["next_at"]:
            state["frame"] = state["frame"] + frames_per_step * state["dir"]
            if state["frame"] >= hi:
                state["frame"] = hi
                state["dir"] = -1
            elif state["frame"] <= lo:
                state["frame"] = lo
                state["dir"] = 1
            stats.requests += 1
            stats.request_times.append((now - stats.started, state["frame"]))
            renderer.request_scrub_preview(state["frame"])
            state["next_at"] = now + interval_ms / 1000.0

    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(2)

    end_at = time.perf_counter() + duration_s + 0.8
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)

    renderer.frame_ready.disconnect(on_ready)
    renderer.finish_scrub(state["frame"])
    # let the settled exact frame land
    end_at = time.perf_counter() + 0.6
    while time.perf_counter() < end_at:
        app.processEvents()
        time.sleep(0.001)

    req_frames = [f for _, f in stats.request_times]
    ready_frames = [f for _, f in stats.ready_times]
    # responsiveness: how much of the drag duration had NO update for >300ms
    stalls = 0
    if stats.ready_times:
        last = stats.ready_times[0][0]
        for t, _ in stats.ready_times[1:]:
            if t - last > 0.3:
                stalls += 1
            last = t
    print(
        f"  {label:8s} req={stats.requests:4d} shown={stats.ready:4d} "
        f"({stats.fps():5.1f}fps shown) stalls>300ms={stalls} "
        f"last_shown={ready_frames[-1] if ready_frames else None} vs final={state['frame']}"
    )
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", default="4K_H264", choices=list(VIDEOS))
    ap.add_argument("--pattern", default=None, choices=list(PATTERNS))
    args = ap.parse_args()

    path = os.path.join(ROOT, VIDEOS[args.video])
    print(f"== e2e scrub bench: {args.video} ==")

    app = QGuiApplication.instance() or QGuiApplication([])
    # GL context for GPU renderer thread
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

    project, seq, total = build_sequence(path)
    provider = Mock()
    provider.request_frame.return_value = None
    provider.open_clip.return_value = "clip-001"

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
    # Real app pauses the playback controller during scrub; mirror that so
    # the monotonic emit guard is bypassed exactly like production.
    renderer.set_playback_controller(
        SimpleNamespace(state=SimpleNamespace(value="paused"))
    )
    print(f"  gpu pipeline: {renderer._has_gpu_preview_pipeline()}")

    # warm the pipeline with one exact render
    renderer.request_render(50)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 5.0:
        app.processEvents()
        time.sleep(0.005)
        if renderer._cache.get(50) is not None:
            break

    patterns = [("all", PATTERNS[args.pattern])] if args.pattern else list(PATTERNS.items())
    for label, (interval_ms, step) in patterns:
        run_pattern(app, renderer, total, label, interval_ms, step)

    renderer.shutdown()
    gr.stop()
    gr.wait(3000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
