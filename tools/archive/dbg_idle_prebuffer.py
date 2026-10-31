#!/usr/bin/env python
"""Debug probe: who bumps _idle_prebuffer_epoch during the idle window?"""
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
        clip_id="clip-001", asset_id=asset_id,
        track_id="track-001", timeline_start=0, timeline_duration=total,
        source_start=0, source_duration=total, speed=1.0,
    )
    track = EditTrack(track_id="track-001", name="V1", track_type="video",
                      index=0, clips=(clip,))
    seq = TimelineSequence(timeline_id="seq-001", name="Bench",
                           fps=Rational(int(fps), 1) if fps else Rational(25, 1),
                           width=width, height=height, video_tracks=(track,))
    return project, seq, total


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

    # --- probe wiring ---
    log = []
    orig_start = TimelinePreviewRenderer._start_idle_prebuffer
    def start(self, anchor):
        log.append(f"[dbg] START anchor={anchor} playing={self._is_controller_playing()} "
                   f"engine={self._engine_playback_active} scrub={self._scrub_active} "
                   f"worker={self._worker is not None}")
        return orig_start(self, anchor)
    TimelinePreviewRenderer._start_idle_prebuffer = start

    orig_tick = TimelinePreviewRenderer._tick_idle_prebuffer
    def tick(self):
        r = orig_tick(self)
        return r
    TimelinePreviewRenderer._tick_idle_prebuffer = tick

    orig_req = TimelinePreviewRenderer.request_render
    def req(self, frame):
        r = orig_req(self, frame)
        return r
    TimelinePreviewRenderer.request_render = req

    # warmup render
    renderer.set_playhead(120)
    renderer.request_render(120)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.0:
        app.processEvents()
        time.sleep(0.002)

    # idle window: watch state every 100ms
    print("=== idle window 2.5s ===", flush=True)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.5:
        app.processEvents()
        time.sleep(0.002)

    cached = [f for f in range(108, 133) if renderer._cache.get(f) is not None]
    print(f"armed={renderer._idle_prebuffer_armed} dispatching={renderer._idle_prebuffer_dispatching} "
          f"pending={sorted(renderer._idle_prebuffer_pending)} cached_frames={cached}", flush=True)
    for line in log:
        print(line, flush=True)

    TimelinePreviewRenderer._start_idle_prebuffer = orig_start
    TimelinePreviewRenderer._tick_idle_prebuffer = orig_tick
    TimelinePreviewRenderer.request_render = orig_req
    renderer.shutdown()
    gr.stop()
    gr.wait(3000)


if __name__ == "__main__":
    main()
