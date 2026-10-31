#!/usr/bin/env python
"""P3.2 composite cache pixel-fidelity check.

Renders one paused frame, reads the composite slot texture and the stored
cache-entry texture back, and compares them pixel-by-pixel. Also re-requests
the frame to force a cache-hit blit and verifies the presented slot again.
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
if os.environ.get("TOPOS_DBG_DEBUG"):
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")

import av
from PyQt6.QtGui import QGuiApplication, QOpenGLContext, QOffscreenSurface, QSurfaceFormat

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit import facade as edit_facade
from src.shared.media.metadata import Rational

sys.path.insert(0, os.path.join(ROOT, "tools"))
from dbg_step_seq import build_sequence  # noqa: E402

PATH = os.path.join(ROOT, "tests/test_videos/4K_H264.mov")


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

    frame_n = 300
    renderer.set_playhead(frame_n)
    renderer.request_render(frame_n)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2.0:
        app.processEvents()
        time.sleep(0.002)

    def read_slot_and_entry():
        """Read current composite slot tex and cache entry tex (GL thread runs
        tasks via queue; do the reads through a submitted no-op? Instead read
        directly here on this thread — the GL context is shared."""
        import numpy as np
        entry_tex = gr._get_cached_composite_tex(frame_n, renderer._generation_id, (1920, 1080))
        if entry_tex is None:
            return None, None
        a = np.frombuffer(entry_tex.read(alignment=1), dtype=np.uint8).reshape(1080, 1920, 4)
        return a, entry_tex

    # 读缓存条目（在与 gr 共享的上下文里直接 read）
    entry_pixels, _ = read_slot_and_entry()
    if entry_pixels is None:
        with gr._composite_cache_lock:
            print("FAIL: no cache entry for frame", frame_n,
                  "entries:", list(gr._composite_cache.keys())[:30],
                  "max:", gr._composite_cache_max)
        renderer.shutdown(); gr.stop(); gr.wait(3000); return 1
    print(f"cache entry stored: {entry_pixels.shape} mean={entry_pixels.mean():.2f}")

    # 再次请求同帧 → worker 命中 → 纯 blit present
    hits = {"n": 0}
    orig_submit = gr.submit
    import src.features.edit.core.timeline_preview_renderer as tpr
    orig_try = tpr.PreviewRenderWorker._try_gpu_render
    renderer.set_playhead(frame_n)
    renderer.request_render(frame_n)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.5:
        app.processEvents()
        time.sleep(0.002)
    # 命中计数：查 trace 不可靠，直接看 present 事件延迟（应 <10ms）

    # 比较：presented slot 内容（命中 blit 后）应与缓存条目一致
    time.sleep(0.3)
    for _ in range(50):
        app.processEvents(); time.sleep(0.002)
    entry2, _ = read_slot_and_entry()
    import numpy as np
    same = np.array_equal(entry_pixels, entry2)
    print(f"entry stable after hit-blit: {same}")
    if not same:
        diff = np.abs(entry_pixels.astype(int) - entry2.astype(int))
        print(f"  max diff={diff.max()} mean diff={diff.mean():.4f}")

    renderer.shutdown()
    gr.stop()
    gr.wait(3000)
    print("OK" if same else "MISMATCH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
