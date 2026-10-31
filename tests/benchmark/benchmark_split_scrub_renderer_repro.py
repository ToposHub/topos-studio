#!/usr/bin/env python3
"""High-fidelity split-scrub reproduction on the REAL renderer stack.

Drives the production TimelinePreviewRenderer exactly like the Edit page /
Color page timeline preview does:

    begin_scrub(f) -> request_scrub_preview(f)@drag cadence -> finish_scrub(f)

with a REAL video asset, REAL GPU decode manager + GPU render thread (same
wiring as file_controller._init_gpu_pipeline), and the production trace
system enabled. It then blade-splits the clip into N segments (timeline_ops
split_clip, exactly what the UI blade tool runs) and repeats the same drag.

Reported per scenario:
  - requested scrub frames
  - real visual updates (frame_ready with a non-None result for a NEW frame
    number, i.e. the picture actually changed)
  - update ratio + updates/sec
  - trace event histogram (decode path, stale drops, warmup pending, ...)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Patch the trace point BEFORE renderer modules import it, so every
# production playback_trace() call lands in our recorder.
import src.features.edit.core.playback_debug as _pbdbg

_TRACE_EVENTS: list[tuple[str, dict]] = []


def _recording_trace(stage: str, **fields) -> None:
    _TRACE_EVENTS.append((stage, fields))


_pbdbg.playback_trace = _recording_trace

import os  # noqa: E402

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402

VIDEO = None  # set in main


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
    assert w._ctx is not None and w._ctx.isValid(), "no GL context"
    return app, w


def import_real_asset(project, video: Path):
    import av

    container = av.open(str(video))
    stream = container.streams.video[0]
    rate = stream.average_rate
    frames = int(stream.frames or 0)
    width, height = stream.codec_context.width, stream.codec_context.height
    container.close()
    metadata = {
        "width": width,
        "height": height,
        "fps": float(rate),
        "fps_num": int(rate.numerator),
        "fps_den": int(rate.denominator),
        "total_frames": frames,
        "duration_seconds": frames / float(rate),
    }
    return project.import_asset(
        path=str(video), media_type="video", metadata=metadata
    )


def build_sequence(project, asset, span_frames: int, fps):
    return build_sequence_multi(project, asset, fps, [(0, 0, span_frames)])


def build_sequence_multi(project, asset, fps, specs):
    """specs: list of (timeline_start, source_start, duration) tuples."""
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.features.edit.core.clip_params import TransformParams, CropParams

    clips = []
    for i, (tstart, sstart, dur) in enumerate(specs):
        clips.append(ClipInstance(
            clip_id=f"clip-{i}",
            asset_id=asset.asset_id,
            track_id="track-1",
            timeline_start=tstart,
            timeline_duration=dur,
            source_start=sstart,
            source_duration=dur,
            speed=1.0,
            transform=TransformParams(),
            crop=CropParams(),
            _source_fps_num=fps.num,
            _source_fps_den=fps.den,
            _timeline_fps_num=fps.num,
            _timeline_fps_den=fps.den,
        ))
    track = EditTrack(
        track_id="track-1", name="Video 1", track_type="video", index=0, clips=tuple(clips)
    )
    return TimelineSequence(name="bench", fps=fps, video_tracks=(track,))


def make_renderer(app, gl_widget, project, sequence):
    from src.features.edit.core.timeline_preview_renderer import (
        TimelinePreviewRenderer,
    )
    from src.shared.video.frame_provider_service import VideoFrameProviderService
    from src.features.edit import facade as edit_facade

    frame_provider = VideoFrameProviderService(
        max_proxy_cache=16, auto_generate_disk_proxies=False
    )
    renderer = TimelinePreviewRenderer(
        project=project,
        sequence=sequence,
        frame_provider=frame_provider,
        viewport_size=(1920, 1080),
    )
    gpu_dm = edit_facade.create_edit_gpu_decode_manager()
    gpu_renderer = edit_facade.create_edit_gpu_preview_renderer(
        main_context=gl_widget._ctx, parent=None
    )
    gpu_renderer.render_error.connect(lambda f, m: print(f"[gpu-err] {f}: {m}"))
    gpu_renderer.start()
    renderer.set_gpu_renderer(gpu_decode_manager=gpu_dm, gpu_renderer=gpu_renderer)
    return renderer, gpu_dm, gpu_renderer


def drag_pattern(total_frames: int, step_frames: int, sweeps: int) -> list[int]:
    """Fast back-and-forth drag across the whole timeline."""
    frames: list[int] = []
    for s in range(sweeps):
        rng = range(0, total_frames, step_frames) if s % 2 == 0 else range(total_frames - 1, -1, -step_frames)
        frames.extend(rng)
    return frames


def run_scrub(app, renderer, frames: list[int], cadence_ms: float, label: str):
    events = {"requested": 0, "emitted": 0, "emitted_none": 0}
    real_updates = []  # (t_ms, frame) for non-None new-frame emissions
    t0 = time.perf_counter()

    def on_ready(frame, result):
        if result is None:
            events["emitted_none"] += 1
            return
        events["emitted"] += 1
        real_updates.append(((time.perf_counter() - t0) * 1000.0, int(frame)))
        # Simulate ProgramViewer consuming the fenced present frame: the
        # viewer blits then acks the slot; without the ack the present pool
        # fills and the GPU renderer drops frames (present_pool_full).
        if isinstance(result, dict) and "ack_cb" in result:
            ack_cb = result["ack_cb"]
            slot_id = getattr(result.get("present_frame"), "slot_id", -1)
            if callable(ack_cb) and slot_id >= 0:
                try:
                    ack_cb(slot_id)
                except Exception:
                    pass

    renderer.frame_ready.connect(on_ready)
    _TRACE_EVENTS.clear()

    renderer.begin_scrub(frames[0])
    last = frames[0]
    next_at = time.perf_counter()
    for f in frames:
        now = time.perf_counter()
        if now < next_at:
            time.sleep(next_at - now)
        renderer.request_scrub_preview(f)
        app.processEvents()
        events["requested"] += 1
        last = f
        next_at = time.perf_counter() + cadence_ms / 1000.0
    renderer.finish_scrub(last)
    # let the settled exact render + async results land
    for _ in range(120):
        app.processEvents()
        time.sleep(0.008)

    renderer.frame_ready.disconnect(on_ready)
    duration_s = time.perf_counter() - t0
    distinct_new = len({f for _, f in real_updates})
    # "画面动了多少": emissions that changed the picture + total frame distance
    transitions = 0
    distance = 0
    prev = None
    for _, f in real_updates:
        if prev is not None:
            if f != prev:
                transitions += 1
            distance += abs(f - prev)
        prev = f
    stages = Counter(stage for stage, _ in _TRACE_EVENTS)
    return {
        "label": label,
        "requested": events["requested"],
        "drag_duration_s": duration_s,
        "real_frame_emissions": events["emitted"],
        "none_emissions": events["emitted_none"],
        "distinct_frames_shown": distinct_new,
        "picture_transitions": transitions,
        "frame_distance_total": distance,
        "updates_per_sec": events["emitted"] / max(duration_s, 1e-6),
        "transitions_per_sec": transitions / max(duration_s, 1e-6),
        "visual_coverage": distinct_new / max(1, len(set(frames))),
        "emission_frames": [f for _, f in real_updates][:400],
        "trace_top": {
            k: v
            for k, v in stages.most_common(24)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--span", type=int, default=200, help="clip length in frames")
    parser.add_argument("--step", type=int, default=3, help="playhead jump per drag tick")
    parser.add_argument("--sweeps", type=int, default=3)
    parser.add_argument("--cadence-ms", type=float, default=10.0)
    parser.add_argument("--segments", type=int, default=4)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    video = Path(args.video).resolve()
    app, glw = build_app_with_gl()

    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_ops import split_clip
    from src.shared.media.metadata import Rational
    import av

    c = av.open(str(video)); s = c.streams.video[0]
    rate = s.average_rate; c.close()
    fps = Rational(int(rate.numerator), int(rate.denominator))

    results = []

    # ---- scenario A: single clip ----
    project = EditProject.create("bench-single")
    project = import_real_asset(project, video)
    asset = project.video_assets[0]
    seq = build_sequence(project, asset, args.span, fps)
    renderer, gpu_dm, gpu_r = make_renderer(app, glw, project, seq)
    # warmup pass (settle decode sources), not measured
    run_scrub(app, renderer, drag_pattern(args.span, max(4, args.step * 2), 1), 30.0, "warmup")
    results.append(
        run_scrub(app, renderer, drag_pattern(args.span, args.step, args.sweeps), args.cadence_ms, "single_clip")
    )

    # ---- scenario B: blade-split into N segments, same total span ----
    seq2 = build_sequence(project, asset, args.span, fps)
    cut_points = [args.span * (i + 1) // args.segments for i in range(args.segments - 1)]
    for cp in cut_points:
        target = next(
            cl for cl in seq2.video_tracks[0].clips
            if cl.timeline_start < cp < cl.timeline_end
        )
        seq2 = split_clip(seq2, target.clip_id, cp)
    renderer.set_sequence(seq2)
    results.append(
        run_scrub(app, renderer, drag_pattern(args.span, args.step, args.sweeps), args.cadence_ms, f"split_{args.segments}seg")
    )

    # ---- scenario C: middle cut out, gaps left open ----
    # (rough cut: delete a middle chunk, no ripple close)
    seg = args.span // args.segments
    gap_specs = [(0, 0, seg)]
    for i in range(1, args.segments):
        tstart = gap_specs[-1][0] + gap_specs[-1][2] + 10  # 10-frame gap
        gap_specs.append((tstart, i * seg, seg))
    total_c = gap_specs[-1][0] + gap_specs[-1][2]
    seq3 = build_sequence_multi(project, asset, fps, gap_specs)
    renderer.set_sequence(seq3)
    results.append(
        run_scrub(app, renderer, drag_pattern(total_c, args.step, args.sweeps), args.cadence_ms, "gaps_open")
    )

    # ---- scenario D: middle cut out, ripple closed (source non-contiguous) ----
    # adjacent timeline segments whose source ranges jump (deleted middles)
    ripple_specs = [(i * seg, i * (seg + 25), seg) for i in range(args.segments)]
    total_d = args.segments * seg
    seq4 = build_sequence_multi(project, asset, fps, ripple_specs)
    renderer.set_sequence(seq4)
    results.append(
        run_scrub(app, renderer, drag_pattern(total_d, args.step, args.sweeps), args.cadence_ms, "ripple_closed")
    )

    try:
        renderer.shutdown()
        gpu_r.stop()
    except Exception:
        pass

    print(json.dumps({
        "video": video.name,
        "fps": f"{fps.num}/{fps.den}",
        "args": vars(args),
        "results": [{k: v for k, v in r.items() if k != "trace_top"} for r in results],
        "traces": [r["trace_top"] for r in results],
    }, indent=2, ensure_ascii=False))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps({
            "video": video.name, "args": vars(args), "results": results,
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
