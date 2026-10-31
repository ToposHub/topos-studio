#!/usr/bin/env python3
"""Scrub latency breakdown on the REAL edit-page renderer stack.

Drives TimelinePreviewRenderer.request_scrub_preview like a real playhead
drag (fast ~10ms and slow ~60ms cadence) with real footage and reports
where the latency goes:

  updates_per_sec   how often the picture actually changed (跟手度)
  e2e_latency_ms    input(request) -> frame_ready for the same frame
  decode_ms         EditGpuDecodeManager.get_frame interactive elapsed
  gpu_submit_ms     worker decode+submit round (worker.gpu_submitted_return)
  decode_frame_type VT vs planar vs cpu vs interactive_stale drops
  defer_busy_ratio  single-flight gate: how many inputs were deferred
                    because a render was still in flight (serialization)

Usage:
  .venv/bin/python tests/benchmark/benchmark_scrub_latency_breakdown.py \
      --video tests/test_videos/4K_prores422HQ.mov
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import src.features.edit.core.playback_debug as _pbdbg

_TRACE: list[tuple[float, str, dict]] = []


def _recording_trace(stage: str, **fields) -> None:
    _TRACE.append((time.perf_counter(), stage, fields))


_pbdbg.playback_trace = _recording_trace

from PyQt6.QtWidgets import QApplication  # noqa: E402


def build_app_with_gl():
    app = QApplication.instance() or QApplication(sys.argv)
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget

    class CtxWidget(QOpenGLWidget):
        def __init__(self):
            super().__init__()
            self._ctx = None
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            self.setFormat(fmt)
            self.resize(160, 90)

        def initializeGL(self):
            self._ctx = self.context()

    w = CtxWidget()
    w.show()
    for _ in range(20):
        app.processEvents()
    assert w._ctx is not None and w._ctx.isValid()
    return app, w


def import_real_asset(project, video: Path):
    import av

    c = av.open(str(video))
    s = c.streams.video[0]
    rate = s.average_rate
    frames = int(s.frames or 0)
    w, h = s.codec_context.width, s.codec_context.height
    c.close()
    metadata = {
        "width": w, "height": h, "fps": float(rate),
        "fps_num": int(rate.numerator), "fps_den": int(rate.denominator),
        "total_frames": frames, "duration_seconds": frames / float(rate),
    }
    return project.import_asset(path=str(video), media_type="video", metadata=metadata)


def pct(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))]


def run_drag(app, renderer, frames, cadence_ms, label):
    inputs: list[tuple[float, int]] = []
    emissions: list[tuple[float, int]] = []
    _TRACE.clear()

    def on_ready(frame, result):
        if result is not None:
            emissions.append((time.perf_counter(), int(frame)))
            if isinstance(result, dict) and "ack_cb" in result:
                try:
                    result["ack_cb"](getattr(result.get("present_frame"), "slot_id", -1))
                except Exception:
                    pass

    renderer.frame_ready.connect(on_ready)
    t0 = time.perf_counter()
    renderer.begin_scrub(frames[0])
    next_at = time.perf_counter()
    for f in frames:
        now = time.perf_counter()
        while now < next_at:
            time.sleep(min(0.002, next_at - now))
            app.processEvents()
            now = time.perf_counter()
        inputs.append((time.perf_counter(), f))
        renderer.request_scrub_preview(f)
        app.processEvents()
        next_at = time.perf_counter() + cadence_ms / 1000.0
    renderer.finish_scrub(frames[-1])
    for _ in range(150):
        app.processEvents()
        time.sleep(0.008)
    renderer.frame_ready.disconnect(on_ready)
    duration = time.perf_counter() - t0

    input_by_frame = defaultdict(list)
    for t, f in inputs:
        input_by_frame[f].append(t)
    e2e = []
    for t, f in emissions:
        cand = [it for it in input_by_frame.get(f, ()) if it <= t]
        if cand:
            e2e.append((t - max(cand)) * 1000.0)

    decode_ms, decode_types = [], Counter()
    stage_by_frame: dict[str, dict[int, float]] = {
        "worker.enqueue": {}, "worker.execute_start": {},
        "decode_manager.get_frame_done": {}, "worker.gpu_submit": {},
        "renderer.scrub_gpu_submitted_release": {},
    }
    for t, stage, fields in _TRACE:
        if stage == "decode_manager.get_frame_done":
            decode_ms.append(float(fields.get("elapsed_ms", 0) or 0))
            decode_types[str(fields.get("frame_type"))] += 1
        mapping = stage_by_frame.get(stage)
        if mapping is not None:
            key = fields.get("frame", fields.get("decoded_frame"))
            if isinstance(key, int):
                mapping.setdefault(key, t)
    # median per-stage gaps across a decode cycle (per frame number)
    def _gap(a: str, b: str) -> float:
        pairs = [
            (stage_by_frame[b][f] - stage_by_frame[a][f]) * 1000.0
            for f in stage_by_frame[a]
            if f in stage_by_frame[b]
        ]
        return round(statistics.median(pairs), 1) if pairs else None

    cycle_gaps = {
        "worker_wake_ms": _gap("worker.enqueue", "worker.execute_start"),
        "decode_ms": _gap("worker.execute_start", "decode_manager.get_frame_done"),
        "submit_ms": _gap("decode_manager.get_frame_done", "worker.gpu_submit"),
        "release_hop_ms": _gap("worker.gpu_submit", "renderer.scrub_gpu_submitted_release"),
    }
    gpu_ms = [
        float(f.get("elapsed_ms", 0) or 0)
        for _, s, f in _TRACE
        if s == "worker.gpu_submitted_return" and f.get("elapsed_ms")
    ]
    stages = Counter(s for _, s, _ in _TRACE)
    n_input = len(inputs)
    defer_busy = stages.get("renderer.scrub_exact_defer_busy", 0)
    stale = sum(v for k, v in stages.items() if "drop" in k or "stale" in k)
    real = len(emissions)
    return {
        "label": label,
        "cadence_ms": cadence_ms,
        "inputs": n_input,
        "duration_s": round(duration, 2),
        "updates_per_sec": round(real / duration, 1),
        "emissions": real,
        "e2e_latency_ms": {
            "p50": round(pct(e2e, 50), 1), "p90": round(pct(e2e, 90), 1),
            "max": round(max(e2e), 1) if e2e else 0,
        },
        "decode_ms": {
            "p50": round(pct(decode_ms, 50), 1), "p90": round(pct(decode_ms, 90), 1),
            "max": round(max(decode_ms), 1) if decode_ms else 0,
        },
        "gpu_round_ms": {"p50": round(pct(gpu_ms, 50), 1), "p90": round(pct(gpu_ms, 90), 1)},
        "cycle_gaps": cycle_gaps,
        "decode_types": dict(decode_types),
        "defer_busy_ratio": round(defer_busy / max(1, n_input), 2),
        "drop_events": stale,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--span", type=int, default=240)
    parser.add_argument("--step-fast", type=int, default=3)
    parser.add_argument("--step-slow", type=int, default=1)
    parser.add_argument("--sweeps", type=int, default=4)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    video = Path(args.video).resolve()
    app, glw = build_app_with_gl()

    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.core.clip_params import TransformParams, CropParams
    from src.shared.media.metadata import Rational
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
    from src.shared.video.frame_provider_service import VideoFrameProviderService
    from src.features.edit import facade as edit_facade
    import av

    c = av.open(str(video)); s = c.streams.video[0]
    rate = s.average_rate; c.close()
    fps = Rational(int(rate.numerator), int(rate.denominator))

    project = EditProject.create("latency-bench")
    project = import_real_asset(project, video)
    asset = project.video_assets[0]
    total = min(args.span, int(asset.metadata["total_frames"]))
    clip = ClipInstance(
        clip_id="clip-1", asset_id=asset.asset_id, track_id="track-1",
        timeline_start=0, timeline_duration=total, source_start=0, source_duration=total,
        speed=1.0, transform=TransformParams(), crop=CropParams(),
        _source_fps_num=fps.num, _source_fps_den=fps.den,
        _timeline_fps_num=fps.num, _timeline_fps_den=fps.den,
    )
    track = EditTrack(track_id="track-1", name="V1", track_type="video", index=0, clips=(clip,))
    seq = TimelineSequence(name="bench", fps=fps, video_tracks=(track,))

    renderer = TimelinePreviewRenderer(
        project=project, sequence=seq,
        frame_provider=VideoFrameProviderService(max_proxy_cache=16, auto_generate_disk_proxies=False),
        viewport_size=(1920, 1080),
    )
    gpu_dm = edit_facade.create_edit_gpu_decode_manager()
    gpu_r = edit_facade.create_edit_gpu_preview_renderer(main_context=glw._ctx, parent=None)
    gpu_r.render_error.connect(lambda f, m: print(f"[gpu-err] {f}: {m}"))
    gpu_r.start()
    renderer.set_gpu_renderer(gpu_decode_manager=gpu_dm, gpu_renderer=gpu_r)

    def pattern(step):
        out = []
        for k in range(args.sweeps):
            rng = range(0, total, step) if k % 2 == 0 else range(total - 1, -1, -step)
            out.extend(rng)
        return out

    run_drag(app, renderer, pattern(8), 40.0, "warmup")  # warm sources
    results = [
        run_drag(app, renderer, pattern(args.step_fast), 10.0, "fast_drag"),
        run_drag(app, renderer, pattern(args.step_slow), 60.0, "slow_drag"),
    ]
    try:
        renderer.shutdown(); gpu_r.stop()
    except Exception:
        pass

    print(json.dumps({"video": video.name, "span": total, "results": results},
                     indent=2, ensure_ascii=False))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps({"video": video.name, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
