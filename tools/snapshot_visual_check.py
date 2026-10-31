#!/usr/bin/env python
"""Capture one real GPU composite snapshot and save it as PNG for visual
inspection (stripe/artifact regression check).

Run: .venv/bin/python tools/snapshot_visual_check.py [video_name]
"""
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
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit import facade as edit_facade
from src.shared.media.metadata import Rational

VIDEO = "tests/test_videos/2K_prores422HQ.mov"


def build_sequence(path: str):
    c = av.open(path)
    s = c.streams.video[0]
    width, height = s.codec_context.width, s.codec_context.height
    num, den = s.average_rate.numerator, s.average_rate.denominator
    total = s.frames or 300
    fps = num / den if den else 25.0
    c.close()
    metadata = {"width": width, "height": height, "fps": fps,
                "total_frames": total, "duration_seconds": total / fps,
                "codec_name": "auto"}
    project = EditProject.create("Bench").import_asset(
        path=os.path.abspath(path), media_type="video", metadata=metadata)
    asset_id = next(iter(project.assets))
    clip = ClipInstance(clip_id="clip-001", asset_id=asset_id, track_id="track-001",
                        timeline_start=0, timeline_duration=total, source_start=0,
                        source_duration=total, speed=1.0)
    track = EditTrack(track_id="track-001", name="V1", track_type="video", index=0, clips=(clip,))
    seq = TimelineSequence(timeline_id="seq-bench", name="Bench", fps=Rational(25, 1),
                           width=width, height=height, video_tracks=(track,))
    return project, seq, total


def main():
    path = os.path.join(ROOT, sys.argv[1] if len(sys.argv) > 1 else VIDEO)
    app = QGuiApplication.instance() or QGuiApplication([])
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    surface = QOffscreenSurface()
    surface.create()
    ctx = QOpenGLContext()
    assert ctx.create()
    ctx.makeCurrent(surface)

    project, seq, total = build_sequence(path)
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
    renderer.set_playback_controller(SimpleNamespace(state=SimpleNamespace(value="paused")))

    saved = {"done": False}

    def on_snapshot(frame, generation, image):
        out = "/tmp/topos_snapshot_check.png"
        image.save(out)
        print(f"snapshot saved: frame={frame} {image.width()}x{image.height()} -> {out}")
        saved["done"] = True

    def on_ready(frame, result):
        if isinstance(result, dict) and result.get("ack_cb"):
            ack = result["ack_cb"]
            slot = getattr(result.get("present_frame"), "slot_id", -1)
            QTimer.singleShot(0, lambda: ack(slot))

    gr.snapshot_ready.connect(on_snapshot)
    renderer.frame_ready.connect(on_ready)

    renderer.request_render(60)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 6.0 and not saved["done"]:
        app.processEvents()
        time.sleep(0.002)

    # also dump the cached entry (what a later seek would display)
    cached = renderer._cache.get(60)
    if cached is not None:
        w, h = cached.width(), cached.height()
        stride_ok = (cached.bytesPerLine() % 4) == 0
        cached.save("/tmp/topos_snapshot_cache.png")
        print(f"cache entry: {w}x{h} stride={cached.bytesPerLine()} aligned4={stride_ok}")
        saved["done"] = True

    if not saved["done"]:
        print("FAILED: no snapshot arrived")
    renderer.shutdown()
    gr.stop()
    gr.wait(3000)
    return 0 if saved["done"] else 1


if __name__ == "__main__":
    sys.exit(main())
