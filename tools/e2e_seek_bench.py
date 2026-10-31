#!/usr/bin/env python
"""End-to-end single-click SEEK benchmark: real TimelinePreviewRenderer +
real EditGpuDecodeManager + real EditGpuPreviewRenderer (GL) + real media.

Mirrors the production paused-seek chain (click on timeline ruler):
  request_render(frame) -> worker decode -> GPU composite -> frame_ready

For every measured seek we record two latencies:
  - first_paint_ms: request -> FIRST frame_ready of any kind (exact frame,
    provisional nearest frame, or async thumbnail placeholder)
  - exact_ms:       request -> frame_ready carrying the requested frame

Patterns:
  nearby   seek to a frame +-3 of a recently rendered neighborhood (the
           "seek back where I just was" case; nearest-frame cache target)
  revisit  seek exactly back onto a recently rendered frame
  far      seek into never-visited regions (cold exact path)

Run:
  .venv/bin/python tools/e2e_seek_bench.py 4K_H264
  .venv/bin/python tools/e2e_seek_bench.py 4K_H264 --pattern nearby --json out.json
"""
from __future__ import annotations

import argparse
import json
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
from src.shared.video.playback_frame_trace import get_trace_collector, reset_trace_collector

VIDEOS = {
    "2K_h264": "tests/test_videos/2K_h264.mp4",
    "2K_H265": "tests/test_videos/2K_H265.mov",
    "2K_prores422HQ": "tests/test_videos/2K_prores422HQ.mov",
    "2K_DNxHR": "tests/test_videos/2K_DNxHR HQX 12-bit.mov",
    "4K_H264": "tests/test_videos/4K_H264.mov",
    "4K_H265": "tests/test_videos/4K_H265.mov",
    "4K_prores422HQ": "tests/test_videos/4K_prores422HQ.mov",
    "4K_DNxHR": "tests/test_videos/4K_DNxHR HQX 12-bit.mov",
}

PATTERNS = ("nearby", "revisit", "far", "step_after_idle")

SEEK_TIMEOUT_S = 4.0


def wait_idle(app, seconds: float):
    """Pump the event loop so QTimer-based idle prebuffer callbacks can fire."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.002)


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


class SeekRecorder:
    """Collects frame_ready emissions around one measured seek."""

    def __init__(self, renderer):
        self.renderer = renderer
        self.events = []          # (t_rel_s, frame, kind)
        self.request_at = None    # perf_counter
        self.target = None
        self._connected = False

    def _on_ready(self, frame, result):
        if self.request_at is None:
            return
        t = time.perf_counter() - self.request_at
        if isinstance(result, dict):
            kind = "gpu_present"
        else:
            # Placeholder thumbnails are <=256px tall; a real CPU preview
            # frame is decode-target sized. Distinguish them so "exact_ms"
            # is not satisfied by a media-pool thumbnail.
            height = getattr(result, "height", lambda: 0)()
            kind = "cpu_frame" if height > 300 else "placeholder"
        self.events.append((t, int(frame), kind))
        # mimic ProgramViewer paint ack for GPU present frames
        if isinstance(result, dict) and result.get("ack_cb"):
            ack = result["ack_cb"]
            slot = getattr(result.get("present_frame"), "slot_id", -1)
            QTimer.singleShot(0, lambda: ack(slot))

    def connect(self):
        self.renderer.frame_ready.connect(self._on_ready)
        self._connected = True

    def disconnect(self):
        if self._connected:
            self.renderer.frame_ready.disconnect(self._on_ready)
            self._connected = False

    def measure(self, app, frame: int) -> dict:
        """Issue one paused seek and wait for the exact frame to land."""
        self.events = []
        self.target = int(frame)
        self.request_at = time.perf_counter()
        self.renderer.set_playhead(int(frame))
        self.renderer.request_render(int(frame))

        deadline = time.perf_counter() + SEEK_TIMEOUT_S
        exact_t = None
        while time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(0.001)
            for t, f, k in self.events:
                if f == self.target and k != "placeholder":
                    exact_t = t
                    break
            if exact_t is not None:
                break
        # small settle window so late provisional frames don't leak into the
        # next measurement
        settle_until = time.perf_counter() + 0.05
        while time.perf_counter() < settle_until:
            app.processEvents()
            time.sleep(0.001)

        first = self.events[0] if self.events else None
        exact_kind = next(
            (k for t, f, k in self.events if f == self.target and k != "placeholder"),
            None,
        )
        first_real = next(
            (e for e in self.events if e[2] != "placeholder"), None
        )
        # P3.2: 全分辨率 GPU 结果到达时间（缓存命中 = 纯 blit，~3-5ms；
        # 未命中 = 完整解码合成，数十至数百 ms）。
        gpu_evt = next(
            (e for e in self.events if e[1] == self.target and e[2] == "gpu_present"),
            None,
        )
        return {
            "frame": self.target,
            "first_paint_ms": round(first[0] * 1000.0, 1) if first else None,
            "first_kind": first[2] if first else None,
            "first_real_ms": round(first_real[0] * 1000.0, 1) if first_real else None,
            "first_is_exact": bool(first and first[1] == self.target and first[2] != "placeholder"),
            "exact_ms": round(exact_t * 1000.0, 1) if exact_t is not None else None,
            "exact_kind": exact_kind,
            "gpu_refine_ms": round(gpu_evt[0] * 1000.0, 1) if gpu_evt else None,
            "n_emissions": len(self.events),
        }


def percentile(values, p):
    if not values:
        return None
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, int(round(p / 100.0 * (len(vs) - 1)))))
    return vs[idx]


def summarize(results):
    firsts = [r["first_paint_ms"] for r in results if r["first_paint_ms"] is not None]
    first_reals = [r["first_real_ms"] for r in results if r["first_real_ms"] is not None]
    exacts = [r["exact_ms"] for r in results if r["exact_ms"] is not None]
    provisional = sum(1 for r in results if r["first_paint_ms"] is not None and not r["first_is_exact"])
    return {
        "n": len(results),
        "first_paint_ms_p50": percentile(firsts, 50),
        "first_paint_ms_p90": percentile(firsts, 90),
        "first_real_ms_p50": percentile(first_reals, 50),
        "first_real_ms_p90": percentile(first_reals, 90),
        "exact_ms_p50": percentile(exacts, 50),
        "exact_ms_p90": percentile(exacts, 90),
        "exact_ms_max": max(exacts) if exacts else None,
        "provisional_first_pct": round(100.0 * provisional / len(results), 1) if results else None,
        "exact_missing": sum(1 for r in results if r["exact_ms"] is None),
    }


def prime_frames(recorder, app, frames):
    """Render a sequence of frames so their neighborhood becomes 'recent'."""
    for f in frames:
        recorder.measure(app, f)


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def build_seek_plan(pattern: str, total: int) -> list[dict]:
    """Return the list of measured seeks (prime steps happen inline)."""
    hi = total - 12
    anchors = [clamp(total * k // 6, 60, hi) for k in range(1, 6)]
    plan = []
    for i, a in enumerate(anchors):
        offset = 3 if i % 2 == 0 else -3
        far_jump = clamp(a + 200 + 40 * i, 60, hi)
        if pattern == "nearby":
            # prime a 5-frame neighborhood, jump far away, then seek to the
            # edge of the neighborhood (uncached but adjacent to recent frames)
            plan.append({"anchor": a, "prime": [a - 2, a - 1, a, a + 1, a + 2],
                         "far": far_jump, "seek": a + offset})
        elif pattern == "revisit":
            plan.append({"anchor": a, "prime": [a - 2, a - 1, a, a + 1, a + 2],
                         "far": far_jump, "seek": a})
        elif pattern == "far":
            # no priming: jump into cold regions both directions
            plan.append({"anchor": a, "prime": [], "far": far_jump,
                         "seek": clamp(a + 137 + 53 * i, 60, hi)})
    return plan


def run_step_after_idle(app, renderer, recorder, total):
    """P3.1 idle prebuffer: pause at anchor, stay idle so the neighborhood
    pre-fills into the frame cache, then step +-1..3 frames.

    Expected DaVinci-like behavior: every step lands on an exact cache hit
    within ~1ms (was a full decode before the idle prebuffer)."""
    hi = total - 40
    anchors = [clamp(total * k // 6, 60, hi) for k in (1, 3, 5)]
    results = []
    fills = []
    for a in anchors:
        recorder.measure(app, a)
        wait_idle(app, 1.8)  # 320ms 静默判定 + ~24 帧填充时间
        radius = 12
        cached = sum(
            1 for f in range(a - radius, a + radius + 1)
            if renderer._cache.get(f) is not None
        )
        fills.append(cached)
        step_frames = (a + 1, a + 2, a + 3, a - 1, a - 2)
        pre = " ".join(
            f"{f}:{'C' if renderer._cache.get(f) is not None else '-'}"
            f"/{'R' if renderer._render_in_flight else 'r'}"
            for f in step_frames
        )
        print(f"    pre-steps@{a}: {pre}")
        for step_frame in step_frames:
            results.append(recorder.measure(app, step_frame))
    s = summarize(results)
    per_step = " ".join(
        f"{r['frame']}:{r['exact_ms']}ms/gpu{r['gpu_refine_ms']}ms" for r in results
    )
    print(f"    steps: {per_step}")
    gpu_refines = [r["gpu_refine_ms"] for r in results if r["gpu_refine_ms"] is not None]
    print(
        f"  step_after_idle n={s['n']} "
        f"first_p50={s['first_paint_ms_p50']}ms p90={s['first_paint_ms_p90']}ms "
        f"| exact_p50={s['exact_ms_p50']}ms p90={s['exact_ms_p90']}ms "
        f"missing={s['exact_missing']} | cache_fill(±12)={fills} "
        f"| gpu_refine_p50={percentile(gpu_refines, 50)}ms p90={percentile(gpu_refines, 90)}ms"
    )
    return {"pattern": "step_after_idle", "summary": s, "samples": results,
            "cache_fill_radius12": fills}


def run_pattern(app, renderer, recorder, pattern, total):
    plan = build_seek_plan(pattern, total)
    results = []
    for step in plan:
        if step["prime"]:
            prime_frames(recorder, app, step["prime"])
        if step["far"] != step["seek"]:
            recorder.measure(app, step["far"])
        results.append(recorder.measure(app, step["seek"]))
    s = summarize(results)
    print(
        f"  {pattern:8s} n={s['n']} "
        f"first_p50={s['first_paint_ms_p50']}ms p90={s['first_paint_ms_p90']}ms "
        f"| first_real_p50={s['first_real_ms_p50']}ms p90={s['first_real_ms_p90']}ms "
        f"provisional={s['provisional_first_pct']}% | "
        f"exact_p50={s['exact_ms_p50']}ms p90={s['exact_ms_p90']}ms "
        f"missing={s['exact_missing']}"
    )
    return {"pattern": pattern, "summary": s, "samples": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", default="4K_H264", choices=list(VIDEOS))
    ap.add_argument("--pattern", default=None, choices=list(PATTERNS))
    ap.add_argument("--json", default=None, help="write results to this JSON file")
    args = ap.parse_args()

    path = os.path.join(ROOT, VIDEOS[args.video])
    print(f"== e2e seek bench: {args.video} ==")

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
    # paused controller like production single-click seek
    renderer.set_playback_controller(
        SimpleNamespace(state=SimpleNamespace(value="paused"))
    )
    print(f"  total_frames={total} gpu_pipeline={renderer._has_gpu_preview_pipeline()}")

    recorder = SeekRecorder(renderer)
    recorder.connect()

    reset_trace_collector()
    # warm the pipeline with one exact render
    r = recorder.measure(app, 50)
    print(f"  warmup frame50: first={r['first_paint_ms']}ms exact={r['exact_ms']}ms kind={r['exact_kind']}")

    patterns = [args.pattern] if args.pattern else list(PATTERNS)
    all_results = []
    for pattern in patterns:
        if pattern == "step_after_idle":
            all_results.append(run_step_after_idle(app, renderer, recorder, total))
        else:
            all_results.append(run_pattern(app, renderer, recorder, pattern, total))

    cache_stats = renderer.get_cache_stats()
    traces = get_trace_collector().get_traces()
    path_counts = {}
    for tr in traces:
        key = f"{getattr(tr, 'decode_path', '')}/{getattr(tr, 'render_path', '')}"
        path_counts[key] = path_counts.get(key, 0) + 1
    print(f"  cpu_cache={cache_stats}")
    print(f"  trace_paths={path_counts}")

    recorder.disconnect()
    renderer.shutdown()
    gr.stop()
    gr.wait(3000)

    if args.json:
        payload = {
            "video": args.video,
            "total_frames": total,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": all_results,
            "cache_stats": cache_stats,
            "trace_paths": path_counts,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"  json -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
