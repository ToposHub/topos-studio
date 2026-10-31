#!/usr/bin/env python
"""Debug: rapid paused steps after idle prebuffer — why does the 2nd step miss the cache?"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import av
from PyQt6.QtGui import QGuiApplication, QOpenGLContext, QOffscreenSurface, QSurfaceFormat

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit import facade as edit_facade
from src.shared.media.metadata import Rational

PATH = os.path.join(ROOT, "tests/test_videos/4K_H264.mov")


def build_sequence(path):
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
        path=os.path.abspath(path), media_type="video", metadata=metadata,
    )
    asset_id = next(iter(project.assets))
    clip = ClipInstance(
        clip_id="clip-001", asset_id=asset_id, track_id="track-001",
        timeline_start=0, timeline_duration=total, source_start=0,
        source_duration=total, speed=1.0,
    )
    track = EditTrack(track_id="track-001", name="V1", track_type="video",
                      index=0, clips=(clip,))
    seq = TimelineSequence(timeline_id="seq-001", name="Bench",
                           fps=Rational(25, 1), width=width, height=height,
                           video_tracks=(track,))
    return project, seq, total


def state(r, tag):
    print(f"  [{tag}] inflight={r._render_in_flight} pending={r._pending_frame} "
          f"queued={r._queued_frame} last_emitted={r._last_emitted_frame} "
          f"snapshot_keys={sorted(k for k in r._snapshot_frame_keys if 118 <= k <= 128)}",
          flush=True)


def main():
    app = QGuiApplication([])
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    surface = QOffscreenSurface()
    surface.create()
    ctx = QOpenGLContext()
    ctx.create()
    ctx.makeCurrent(surface)

    project, seq, total = build_sequence(PATH)
    provider = Mock()
    provider.request_frame.return_value = None
    provider.open_clip.return_value = "clip-001"

    renderer = TimelinePreviewRenderer(
        project=project, sequence=seq, frame_provider=provider,
        main_context=ctx, viewport_size=(1920, 1080),
    )
    dm = edit_facade.create_edit_gpu_decode_manager()
    gr = edit_facade.create_edit_gpu_preview_renderer(main_context=ctx)
    gr.start()
    renderer.set_gpu_renderer(gpu_decode_manager=dm, gpu_renderer=gr)
    renderer.set_comp_gl_context(ctx)
    renderer.set_playback_controller(SimpleNamespace(state=SimpleNamespace(value="paused")))

    anchor = 400
    renderer.set_playhead(anchor)
    renderer.request_render(anchor)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.0:
        app.processEvents()
        time.sleep(0.002)

    print(f"=== idle 2.5s at anchor {anchor} ===", flush=True)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.5:
        app.processEvents()
        time.sleep(0.002)
    cached = [f for f in range(anchor - 12, anchor + 13) if renderer._cache.get(f) is not None]
    print(f"cached={cached}", flush=True)

    events = []
    def on_ready(f, r):
        h = r.height() if hasattr(r, "height") and not isinstance(r, dict) else -1
        events.append((round((time.perf_counter() - step_t0) * 1000, 1), int(f),
                       "gpu" if isinstance(r, dict) else f"img{h}"))
    renderer.frame_ready.connect(on_ready)

    global step_t0
    for target in (anchor + 1, anchor + 2, anchor + 3, anchor - 1, anchor - 2):
        state(renderer, f"before step {target}")
        print(f"  cache.get({target})={'HIT' if renderer._cache.get(target) is not None else 'MISS'} "
              f"should_emit={renderer._should_emit_frame(target)}", flush=True)
        events.clear()
        step_t0 = time.perf_counter()
        renderer.set_playhead(target)
        renderer.request_render(target)
        # bench semantics: wait for exact frame, then 50ms settle
        while time.perf_counter() - step_t0 < 1.0:
            app.processEvents()
            time.sleep(0.001)
            if any(f == target and k != "img256" for _, f, k in events):
                break
        settle = time.perf_counter() + 0.05
        while time.perf_counter() < settle:
            app.processEvents()
            time.sleep(0.001)
        print(f"  step {target} events={events}", flush=True)
        state(renderer, f"after step {target}")

    renderer.frame_ready.disconnect(on_ready)
    with gr._composite_cache_lock:
        entries = list(gr._composite_cache.keys())
        print(f"composite_cache: {len(entries)} entries max={gr._composite_cache_max} keys={entries[:30]}", flush=True)
    renderer.shutdown()
    gr.stop()
    gr.wait(3000)


if __name__ == "__main__":
    main()
